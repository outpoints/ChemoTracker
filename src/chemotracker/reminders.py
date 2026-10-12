"""Phone reminders through ntfy (https://ntfy.sh): free, no account, and self-hostable.

Everyone in the family installs the ntfy app and subscribes to the same topic.
A background thread checks once every 30 seconds for:

- prep:  "Take Filgrastim out of the fridge now" (the medicine's prep minutes before the dose)
- due:   "9:30 PM medicines: Olanzapine 1 tablet"
- nag:   "Not marked yet" when a dose still isn't marked taken or skipped some minutes later
- chemo: "Chemo tomorrow" at 6 PM the day before Day 1
- ok:    "Prochlorperazine is OK again" for an as-needed medicine someone ticked in Settings, once the label's
         gap (and its rolling 24-hour limit) has passed since the last dose. Never "due": it's only if you need it.
- morning: one list of those medicines at the chosen morning time. An OK time inside the overnight hold
         (from ntfy_quiet_from until the morning reminder) waits for it instead of waking anyone.

Each reminder has a key stored in `reminders_sent`, so a restart never repeats one, and
a reminder older than CATCH_UP is dropped instead of arriving late in a burst. Every window
is measured in real time, so the nights the clocks change neither skip a dose nor send one early.
The doses come from schedule.slots_on, the same list Today shows.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from . import schedule
from .config import Settings
from .db import get_settings, session, utcnow

log = logging.getLogger("chemotracker.reminders")

CATCH_UP = timedelta(minutes=45)
CHEMO_EVE = time(18, 0)
TICK_SECONDS = 30
DEFAULT_SERVER = "https://ntfy.sh"
# How a crash while working out the reminders starts in last_error, so Settings can say it in plain words.
CHECK_FAILED = "reminder check failed"
# As-needed medicines: the morning reminder (6:00 AM to 12:00 PM) and when the overnight hold starts (9:00 PM to
# 6:00 AM), in 30-minute steps. "off" turns either off.
MORNING_DEFAULT = "09:00"
QUIET_DEFAULT = "02:00"
MORNING_CHOICES = tuple(f"{h:02d}:{m:02d}" for h in range(6, 13) for m in (0, 30) if (h, m) <= (12, 0))
QUIET_CHOICES = tuple(f"{h % 24:02d}:{m:02d}" for h in range(21, 31) for m in (0, 30) if (h, m) <= (30, 0))
ONLY_IF = "Take it only if you need it, and follow the label."


@dataclass
class Reminder:
    key: str
    title: str
    message: str
    tags: tuple[str, ...]
    priority: int = 4
    click: str | None = None
    also: tuple[str, ...] = field(default_factory=tuple)  # more keys to record as sent (one per dose in a group)


def new_topic() -> str:
    """Long and random: on ntfy.sh, anyone who knows a topic name can read it."""
    return "chemotracker-" + secrets.token_urlsafe(12).replace("_", "").replace("-", "").lower()[:14]


def enabled(prefs: dict[str, str]) -> bool:
    return prefs.get("ntfy_enabled") == "1" and bool(prefs.get("ntfy_topic"))


def ok_med_ids(prefs: dict[str, str]) -> set[int]:
    """The medicines ticked for \"Tell me when it's OK again\" (ntfy_ok_meds, comma-separated ids)."""
    return {int(p) for p in (prefs.get("ntfy_ok_meds") or "").split(",") if p.strip().isdigit()}


def ok_choices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Medicines Settings offers for \"Tell me when it's OK again\": active ones with an as-needed plan (stopped and
    archived ones aren't listed). Only those with the hours between doses saved can be ticked."""
    return conn.execute(
        """SELECT m.id, m.name, m.min_hours_between FROM medications m WHERE m.active = 1
             AND EXISTS (SELECT 1 FROM plans p WHERE p.medication_id = m.id AND p.kind = 'as_needed')
           ORDER BY m.name COLLATE NOCASE, m.id"""
    ).fetchall()


def _clock(value: str | None, default: str) -> time | None:
    if value == "off":
        return None
    try:
        return time.fromisoformat(value or default)
    except ValueError:
        return time.fromisoformat(default)


def morning_at(prefs: dict[str, str]) -> time | None:
    """The morning reminder's time, or None when it's off."""
    return _clock(prefs.get("ntfy_morning"), MORNING_DEFAULT)


def held(at: datetime, prefs: dict[str, str]) -> bool:
    """Whether `at` falls in the overnight hold: from ntfy_quiet_from until the morning reminder (it may cross
    midnight). With the morning reminder off nothing is held, because nothing would tell anyone later."""
    start, end = _clock(prefs.get("ntfy_quiet_from"), QUIET_DEFAULT), morning_at(prefs)
    if start is None or end is None or start == end:
        return False
    clock = at.time()
    return start <= clock < end if start < end else (clock >= start or clock < end)


def _on_day(d, today) -> str:
    if d == today:
        return ""
    return "yesterday " if (today - d).days == 1 else f"on {schedule.fmt_day(d)} "


def _ok_at_text(t: datetime, now: datetime) -> str:
    """'at 11:20 AM', 'tomorrow at 1:00 AM'."""
    if t.date() == now.date():
        return f"at {schedule.fmt_time(t)}"
    if (t.date() - now.date()).days == 1:
        return f"tomorrow at {schedule.fmt_time(t)}"
    return f"on {schedule.fmt_day(t.date())} at {schedule.fmt_time(t)}"


def _as_needed_ticked(conn: sqlite3.Connection, prefs: dict[str, str], now: datetime
                      ) -> list[tuple[schedule.AsNeeded, datetime | None]]:
    """Each ticked medicine that is active with a gap saved, built the way Today's shelf builds it, with when the
    next one is OK (the gap, moved later while the rolling 24-hour limit is reached)."""
    out = []
    for mid in sorted(ok_med_ids(prefs)):
        med = conn.execute("SELECT * FROM medications WHERE id = ? AND active = 1", (mid,)).fetchone()
        if med is None or not med["min_hours_between"]:
            continue
        an = schedule._as_needed_row(conn, med, None, now)
        out.append((an, schedule.ok_again_at(conn, an)))
    out.sort(key=lambda pair: (pair[0].name.lower(), pair[0].medication_id))
    return out


def _ok_key(an: schedule.AsNeeded, at: datetime) -> str:
    return f"ok|{an.medication_id}|{an.last['id']}|{at:%Y-%m-%d %H:%M}"


def shelf_notes(conn: sqlite3.Connection, prefs: dict[str, str], now: datetime,
                shelf: list[schedule.AsNeeded]) -> dict[int, str]:
    """The quiet line under a ticked medicine on Today's as-needed shelf, saying when the phone will hear about it.
    Nothing when no reminder will come (reminders off, not planned, no gap, or the next OK time is after the plan)."""
    if not enabled(prefs):
        return {}
    ticked = ok_med_ids(prefs)
    morning = morning_at(prefs)
    out: dict[int, str] = {}
    for an in shelf:
        # Only while a reminder is pending. Under "OK to take now" a line about being told later reads as a
        # contradiction, so a medicine that's already OK shows nothing.
        if an.medication_id not in ticked or not an.gap_known or an.state(now) not in ("wait", "max"):
            continue
        at = schedule.ok_again_at(conn, an)
        if at is None or not schedule.prn_covers(conn, an.medication_id, at.date()):
            continue
        if morning is not None and held(at, prefs):
            told = datetime.combine(at.date(), morning)  # the morning list at or after it
            at = told if schedule.real_diff(told, at) >= timedelta(0) else told + timedelta(days=1)
        out[an.medication_id] = f"We'll tell you {_ok_at_text(at, now)}"
    return out


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def _slots_on(conn: sqlite3.Connection, d) -> list[schedule.Slot]:
    """Every home dose with a set time on date `d`: the same doses Today shows, each once."""
    return [s for s in schedule.slots_on(conn, d) if s.kind == "scheduled" and s.time]


def _recent_note(conn: sqlite3.Connection, group: list[schedule.Slot], now: datetime, names: bool) -> str:
    """A line about a dose of the same medicine logged earlier that day (or within its label gap), which isn't
    counted for this dose: given early from "Log a dose", or on a day it's also allowed as needed. The phone must
    not read like nothing was given."""
    lines = []
    for s in group:
        window = max(timedelta(hours=s.plan["med_gap"] or 0), timedelta(hours=2),
                     min(timedelta(hours=12), s.dt - datetime.combine(s.dt.date(), time(0, 0))))
        row = conn.execute(
            """SELECT id, taken_at FROM dose_logs WHERE medication_id = ? AND status = 'taken' AND taken_at <= ?
                AND taken_at >= ? ORDER BY taken_at DESC""",
            (s.plan["medication_id"], schedule.fmt_local(now + schedule.AHEAD_OK),
             schedule.fmt_local(s.dt - window - timedelta(hours=1))),
        ).fetchall()
        for r in row:
            if schedule.paired_slot(conn, r["id"]) is not None:
                continue
            taken = schedule.parse_local(r["taken_at"])
            if taken is None or schedule.real_diff(s.dt, taken) > window:
                continue
            day = "" if taken.date() == s.dt.date() else f"{schedule.fmt_relative_day(taken.date(), s.dt.date()).lower()} "
            who = s.plan["med_name"] if names else "One of these medicines"
            lines.append(f"{who} was logged {day}at {schedule.fmt_time(taken)}. Check before giving another dose.")
            break
    return "\n".join(dict.fromkeys(lines))


def _what(slot: schedule.Slot) -> str:
    dose = slot.dose
    return f"{slot.plan['med_name']} {dose}".strip() if dose else slot.plan["med_name"]


def due_reminders(conn: sqlite3.Connection, now: datetime, prefs: dict[str, str]) -> list[Reminder]:
    """What should go out now. Pure: reads the database, sends nothing.

    `now` is local wall-clock time; on the night the clocks go back, its `fold` says which 1:30 AM it is.
    """
    names = prefs.get("ntfy_names", "1") == "1"
    lead = timedelta(minutes=_int(prefs.get("ntfy_lead"), 0))
    nag_after = _int(prefs.get("ntfy_nag"), 30)
    url = (prefs.get("app_url") or "").rstrip("/") or None
    sent = {r[0] for r in conn.execute("SELECT key FROM reminders_sent")}  # keys carry their date
    out: list[Reminder] = []
    # Tomorrow too: a dose just after midnight needs its prep step (or an early reminder) before midnight.
    slots = [s for d in (now.date() - timedelta(days=1), now.date(), now.date() + timedelta(days=1))
             for s in _slots_on(conn, d)]
    open_slots = [s for s in slots if s.log is None]

    def since(at: datetime) -> timedelta:
        """Real time from `at` until now (negative before it)."""
        return schedule.real_diff(now, at)

    # Prep steps (each medicine on its own: different medicines need different things).
    for s in open_slots:
        at = s.prep_at
        if at and timedelta(0) <= since(at) < CATCH_UP and since(s.dt) < timedelta(0):
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
        stamp = f"{at:%Y-%m-%d %H:%M}"
        nag_now = bool(nag_after) and timedelta(minutes=nag_after) <= since(at) < timedelta(minutes=nag_after) + CATCH_UP
        caveat = _recent_note(conn, group, now, names)
        # Once it's time for "not marked yet", a due reminder that never went out (server down) is skipped.
        if -lead <= since(at) < CATCH_UP - lead and not nag_now:
            members = _unsent(group, "due", stamp, sent)
            if members:
                key = f"due|{stamp}" if f"due|{stamp}" not in sent else \
                    f"due|{stamp}|" + ",".join(str(s.plan["id"]) for s in members)
                if names:
                    title = f"{members[0].plan['med_name']} at {when}" if len(members) == 1 else f"{when} medicines"
                    msg = "\n".join(_what(s) for s in members) + (f"\n{caveat}" if caveat else "") + \
                        "\nMark it in ChemoTracker once it's taken."
                else:
                    title = f"Medicine due at {when}"
                    msg = (f"{caveat}\n" if caveat else "") + "Open ChemoTracker to see what's due and mark it taken."
                out.append(Reminder(key, title, msg, ("pill",), 4, url,
                                    tuple(f"due|{stamp}|{s.plan['id']}" for s in members)))
        if nag_now:
            members = _unsent(group, "nag", stamp, sent)
            if members:
                key = f"nag|{stamp}" if f"nag|{stamp}" not in sent else \
                    f"nag|{stamp}|" + ",".join(str(s.plan["id"]) for s in members)
                title = f"Not marked yet: {when}"
                msg = ((", ".join(s.plan["med_name"] for s in members) + " isn't marked taken or skipped yet. "
                        "If it was taken, mark it so everyone knows.") if names
                       else "A dose isn't marked taken or skipped yet. If it was taken, mark it so everyone knows.")
                if caveat:
                    msg += f"\n{caveat}"
                out.append(Reminder(key, title, msg, ("warning",), 3, url,
                                    tuple(f"nag|{stamp}|{s.plan['id']}" for s in members)))

    # Chemo tomorrow.
    if prefs.get("ntfy_chemo", "1") == "1" and now.time() >= CHEMO_EVE:
        tomorrow = now.date() + timedelta(days=1)
        cycle = conn.execute("SELECT * FROM cycles WHERE day1 = ?", (tomorrow.isoformat(),)).fetchone()
        if cycle:
            title = f"Chemo tomorrow: cycle {cycle['number']}" if names else "Appointment tomorrow"
            msg = (f"Day 1 is tomorrow, {schedule.fmt_day(tomorrow)}. Check Today for anything to take tonight "
                   "or in the morning." if names else "Open ChemoTracker to see what's planned.")
            out.append(Reminder(f"chemo|{tomorrow.isoformat()}", title, msg, ("calendar",), 3, url))

    out += _as_needed_reminders(conn, now, prefs, names, url)

    unique: dict[str, Reminder] = {}
    for r in out:
        if r.key not in sent:
            unique.setdefault(r.key, r)
    return list(unique.values())


def _as_needed_reminders(conn: sqlite3.Connection, now: datetime, prefs: dict[str, str], names: bool,
                         url: str | None) -> list[Reminder]:
    """The morning list and \"OK again\" for ticked as-needed medicines. They say a medicine is OK again if you need
    it, never that it's due: nothing is missed when nobody takes one."""
    ticked = _as_needed_ticked(conn, prefs, now) if ok_med_ids(prefs) else []
    if not ticked:
        return []
    click = f"{url}/#c-prn" if url else None
    out: list[Reminder] = []
    covered: set[str] = set()

    morning = morning_at(prefs)
    if morning is not None:
        at = datetime.combine(now.date(), morning)
        if timedelta(0) <= schedule.real_diff(now, at) < CATCH_UP:
            start = _clock(prefs.get("ntfy_quiet_from"), QUIET_DEFAULT)
            quiet_start = None
            if start is not None:
                quiet_start = datetime.combine(at.date(), start) - (timedelta(days=1) if start > morning else timedelta(0))
            lines = []
            for an, ok_at in ticked:
                if not schedule.prn_covers(conn, an.medication_id, now.date()):
                    continue
                state = an.state(now)
                if state == "ok":
                    text = "OK if you need it"
                    if ok_at is not None and an.last is not None:
                        covered.add(_ok_key(an, ok_at))  # this list tells it, so the phone doesn't buzz twice
                        if quiet_start is not None and held(ok_at, prefs) and \
                                schedule.real_diff(ok_at, quiet_start) >= timedelta(0) > schedule.real_diff(ok_at, at):
                            text += f", since {schedule.fmt_time(ok_at)}"
                elif state == "wait" and ok_at is not None:
                    text = f"OK {_ok_at_text(ok_at, now)}"
                elif state == "max":
                    text = f"Reached {an.max_per_day} in 24 hours"
                    if ok_at is not None and schedule.prn_covers(conn, an.medication_id, ok_at.date()):
                        text += f", OK {_ok_at_text(ok_at, now)}"
                else:
                    text = an.status_text(now)
                lines.append(f"{an.name}: {text}")
            if lines:
                msg = ("\n".join(lines) + "\nTake one only if you need it, and follow the label." if names
                       else "Open ChemoTracker to see when each one is OK.")
                out.append(Reminder(f"morning|{now.date().isoformat()}", "Your as-needed medicines", msg, ("sunrise",), 3,
                                    click, tuple(sorted(covered))))

    for an, ok_at in ticked:
        if ok_at is None or an.last is None or an.last_at is None:
            continue
        if not timedelta(0) <= schedule.real_diff(now, ok_at) < CATCH_UP:
            continue
        if held(ok_at, prefs) or held(now, prefs):  # overnight: the morning reminder lists it instead
            continue
        if not schedule.prn_covers(conn, an.medication_id, ok_at.date()):
            continue  # the plan has ended (after Day 7, say): nothing to tell
        key = _ok_key(an, ok_at)
        if key in covered:
            continue
        if names:
            ago = schedule.fmt_duration(schedule.real_diff(now, schedule.latest_reading(an.last_at)))
            title = f"{an.name} is OK again"
            msg = (f"It's been {ago} since the last one {_on_day(an.last_at.date(), now.date())}at "
                   f"{schedule.fmt_time(an.last_at)}. {ONLY_IF}")
        else:
            title, msg = "A medicine is OK again", "Open ChemoTracker to see which one."
        out.append(Reminder(key, title, msg, ("hourglass",), 3, click))
    return out


def _unsent(group: list[schedule.Slot], kind: str, stamp: str, sent: set[str]) -> list[schedule.Slot]:
    """The doses in a group that this reminder hasn't covered yet.

    The first reminder for a time covers every dose then and records one key per dose. A dose that joins that time
    later (its schedule was moved onto it) gets its own reminder. Reminders sent before per-dose keys existed
    covered the whole group.
    """
    if f"{kind}|{stamp}" not in sent:
        return group
    if not any(k.startswith(f"{kind}|{stamp}|") for k in sent):
        return []
    return [s for s in group if f"{kind}|{stamp}|{s.plan['id']}" not in sent]


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
    if e.startswith(CHECK_FAILED):
        return ("Something went wrong in ChemoTracker while checking for reminders, so they may not go out. "
                "It tries again every 30 seconds. If this message stays, restart ChemoTracker.")
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

    def _failed(self, exc: Exception) -> None:
        """A crash in the check is shown in Settings (it used to be only in the log, so Settings looked fine)."""
        self.last_error, self.last_error_at = f"{CHECK_FAILED}: {type(exc).__name__}: {exc}", self.now()
        log.exception("reminder check failed")

    def tick(self, sender=send) -> int:
        """One pass. Returns how many reminders went out."""
        try:
            with session(self.settings.db_path) as conn:
                prefs = get_settings(conn)
                due = due_reminders(conn, self.now(), prefs) if enabled(prefs) else []
        except Exception as exc:  # noqa: BLE001 - shown in Settings, tried again next tick
            self._failed(exc)
            return 0
        if self.last_error and self.last_error.startswith(CHECK_FAILED):
            self.last_error = None  # the check works again. A send problem stays until a reminder goes through.
        count = 0
        for r in due:
            try:
                sender(prefs, r)
            except Exception as exc:  # noqa: BLE001 - shown in Settings, retried next tick until CATCH_UP passes
                self.last_error, self.last_error_at = str(exc), self.now()
                log.warning("reminder %s not sent: %s", r.key, exc)
                break  # the server is probably down; try again next tick
            with session(self.settings.db_path, write=True) as conn:
                for key in (r.key, *r.also):
                    conn.execute("INSERT OR IGNORE INTO reminders_sent (key, sent_at) VALUES (?, ?)", (key, utcnow()))
                cutoff = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
                conn.execute("DELETE FROM reminders_sent WHERE sent_at < ?", (cutoff,))
            self.last_sent, self.last_error = self.now(), None
            count += 1
        return count

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - never let the thread die, and show it in Settings
                self._failed(exc)
