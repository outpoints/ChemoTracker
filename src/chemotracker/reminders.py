"""Phone reminders through ntfy (https://ntfy.sh): free, no account, and self-hostable.

Everyone in the family installs the ntfy app and subscribes to the same topic.
A background thread checks once every 30 seconds for:

- prep:  "Take Filgrastim out of the fridge now" (the medicine's prep minutes before the dose)
- due:   "9:30 PM medicines: Olanzapine 1 tablet"
- nag:   "Not marked yet" when a dose still isn't marked taken or skipped some minutes later
- chemo: "Chemo tomorrow" at 6 PM the day before Day 1

Each reminder has a key stored in `reminders_sent`, so a restart never repeats one, and
a reminder older than CATCH_UP is dropped instead of arriving late in a burst.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from . import schedule
from .config import Settings
from .db import get_settings, session, utcnow

log = logging.getLogger("chemotracker.reminders")

CATCH_UP = timedelta(minutes=45)
CHEMO_EVE = time(18, 0)
TICK_SECONDS = 30
DEFAULT_SERVER = "https://ntfy.sh"


@dataclass
class Reminder:
    key: str
    title: str
    message: str
    tags: tuple[str, ...]
    priority: int = 4
    click: str | None = None


def new_topic() -> str:
    """Long and random: on ntfy.sh, anyone who knows a topic name can read it."""
    return "chemotracker-" + secrets.token_urlsafe(12).replace("_", "").replace("-", "").lower()[:14]


def enabled(prefs: dict[str, str]) -> bool:
    return prefs.get("ntfy_enabled") == "1" and bool(prefs.get("ntfy_topic"))


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def _slots_on(conn: sqlite3.Connection, d) -> list[schedule.Slot]:
    """Every home dose with a set time on date `d`, across cycles (Day 0 belongs to the next cycle)."""
    seen, out = set(), []
    for cycle in (schedule.cycle_for_date(conn, d), schedule.next_cycle_after(conn, d),
                  schedule.cycle_for_date(conn, d - timedelta(days=1))):
        if cycle is None or cycle["id"] in seen:
            continue
        seen.add(cycle["id"])
        out += [s for s in schedule.slots(conn, cycle) if s.date == d and s.kind == "scheduled" and s.time]
    return out


def _what(slot: schedule.Slot) -> str:
    dose = slot.dose
    return f"{slot.plan['med_name']} {dose}".strip() if dose else slot.plan["med_name"]


def due_reminders(conn: sqlite3.Connection, now: datetime, prefs: dict[str, str]) -> list[Reminder]:
    """What should go out now. Pure: reads the database, sends nothing."""
    names = prefs.get("ntfy_names", "1") == "1"
    lead = timedelta(minutes=_int(prefs.get("ntfy_lead"), 0))
    nag_after = _int(prefs.get("ntfy_nag"), 30)
    url = (prefs.get("app_url") or "").rstrip("/") or None
    out: list[Reminder] = []
    slots = [s for d in (now.date() - timedelta(days=1), now.date()) for s in _slots_on(conn, d)]
    open_slots = [s for s in slots if s.log is None]

    # Prep steps (each medicine on its own: different medicines need different things).
    for s in open_slots:
        at = s.prep_at
        if at and at <= now < at + CATCH_UP and now < s.dt:
            note = s.plan["med_prep_note"] or "Get it ready"
            title = f"{note}: {s.plan['med_name']}" if names else "Get a medicine ready"
            msg = (f"{note} now. The dose is at {schedule.fmt_time(s.dt)}." if names
                   else f"One is due at {schedule.fmt_time(s.dt)}. Open ChemoTracker to see what to do.")
            out.append(Reminder(f"prep|{s.plan['id']}|{s.key}", title, msg, ("hourglass_flowing_sand",), 4, url))

    # Doses due, grouped by time so 8:00 AM is one notification, not three.
    by_time: dict[datetime, list[schedule.Slot]] = {}
    for s in open_slots:
        by_time.setdefault(s.dt, []).append(s)
    for at, group in sorted(by_time.items()):
        when = schedule.fmt_time(at)
        nag_from = at + timedelta(minutes=nag_after) if nag_after else None
        nag_now = nag_from is not None and nag_from <= now < nag_from + CATCH_UP
        # Once it's time for "not marked yet", a due reminder that never went out (server down) is skipped.
        if at - lead <= now < at - lead + CATCH_UP and not nag_now:
            if names:
                title = f"{group[0].plan['med_name']} at {when}" if len(group) == 1 else f"{when} medicines"
                msg = "\n".join(_what(s) for s in group) + "\nMark it in ChemoTracker once it's taken."
            else:
                title, msg = f"Medicine due at {when}", "Open ChemoTracker to see what's due and mark it taken."
            out.append(Reminder(f"due|{at:%Y-%m-%d %H:%M}", title, msg, ("pill",), 4, url))
        if nag_now:
            title = f"Not marked yet: {when}"
            msg = ((", ".join(s.plan["med_name"] for s in group) + " isn't marked taken or skipped yet. "
                    "If it was taken, mark it so everyone knows.") if names
                   else "A dose isn't marked taken or skipped yet. If it was taken, mark it so everyone knows.")
            out.append(Reminder(f"nag|{at:%Y-%m-%d %H:%M}", title, msg, ("warning",), 3, url))

    # Chemo tomorrow.
    if prefs.get("ntfy_chemo", "1") == "1" and now.time() >= CHEMO_EVE:
        tomorrow = now.date() + timedelta(days=1)
        cycle = conn.execute("SELECT * FROM cycles WHERE day1 = ?", (tomorrow.isoformat(),)).fetchone()
        if cycle:
            title = f"Chemo tomorrow: cycle {cycle['number']}" if names else "Appointment tomorrow"
            msg = (f"Day 1 is tomorrow, {schedule.fmt_day(tomorrow)}. Check Today for anything to take tonight "
                   "or in the morning." if names else "Open ChemoTracker to see what's planned.")
            out.append(Reminder(f"chemo|{tomorrow.isoformat()}", title, msg, ("calendar",), 3, url))

    sent = {r[0] for r in conn.execute("SELECT key FROM reminders_sent")}  # keys carry their date
    return [r for r in out if r.key not in sent]


def send(prefs: dict[str, str], r: Reminder, timeout: float = 10) -> None:
    """Publish one notification. Raises on failure."""
    server = (prefs.get("ntfy_server") or DEFAULT_SERVER).rstrip("/")
    body: dict = {"topic": prefs["ntfy_topic"], "title": r.title, "message": r.message,
                  "tags": list(r.tags), "priority": r.priority}
    if r.click:
        body["click"] = r.click
        body["actions"] = [{"action": "view", "label": "Open ChemoTracker", "url": r.click}]
    req = urllib.request.Request(server, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "ChemoTracker"})
    token = prefs.get("ntfy_token")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured ntfy server
            if resp.status >= 300:
                raise RuntimeError(f"ntfy answered {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode(errors="replace")
        raise RuntimeError(f"ntfy answered {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"couldn't reach {server} ({exc.reason})") from None


def problem_text(error: str | None) -> str:
    """What a failed reminder means for the family, in plain words. The raw error stays available as details."""
    e = (error or "").lower()
    if "couldn't reach" in e:
        return ("The reminder server didn't answer. Check that this computer is online, "
                "and that the server address under Advanced is right.")
    if "answered 401" in e or "answered 403" in e:
        return "The reminder server turned it down. Check the access token under Advanced, or ask whoever set it up."
    if "answered 429" in e:
        return "Too many reminders went out at once. They'll start again on their own in a few minutes."
    if "answered 5" in e:
        return "The reminder server had a problem on its side. It'll try again with the next reminder."
    return "The last reminder didn't go through. It'll try again with the next one."


def test_reminder(prefs: dict[str, str]) -> Reminder:
    url = (prefs.get("app_url") or "").rstrip("/") or None
    return Reminder("test", "ChemoTracker test", "Reminders work on this phone.", ("white_check_mark",), 3, url)


class ReminderScheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sent: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def now(self) -> datetime:
        return datetime.now(self.settings.tz).replace(tzinfo=None, second=0, microsecond=0)

    def tick(self, sender=send) -> int:
        """One pass. Returns how many reminders went out."""
        with session(self.settings.db_path) as conn:
            prefs = get_settings(conn)
            if not enabled(prefs):
                return 0
            due = due_reminders(conn, self.now(), prefs)
        count = 0
        for r in due:
            try:
                sender(prefs, r)
            except Exception as exc:  # noqa: BLE001 - shown in Settings, retried next tick until CATCH_UP passes
                self.last_error, self.last_error_at = str(exc), self.now()
                log.warning("reminder %s not sent: %s", r.key, exc)
                break  # the server is probably down; try again next tick
            with session(self.settings.db_path, write=True) as conn:
                conn.execute("INSERT OR IGNORE INTO reminders_sent (key, sent_at) VALUES (?, ?)", (r.key, utcnow()))
                cutoff = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
                conn.execute("DELETE FROM reminders_sent WHERE sent_at < ?", (cutoff,))
            self.last_sent, self.last_error = self.now(), None
            count += 1
        return count

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never let the thread die
                log.exception("reminder check failed")
