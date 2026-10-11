"""Cycle-day arithmetic, dose slots, and the views built on them.

Planned doses are not stored. They are computed from each cycle's Day 1 and
the medicine plans, then matched to dose_logs by (plan_id, slot). Editing a
plan therefore never leaves stale "planned" rows behind.

Day numbering follows the clinic sheet: Day 1 is chemo day, Day 0 is the day
before chemo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import symptoms as sx

DUE_BEFORE = timedelta(minutes=30)
DUE_AFTER = timedelta(minutes=60)
FOLLOW_UP_AFTER = timedelta(minutes=30)
FOLLOW_UP_WITHIN = timedelta(hours=24)

# Time-of-day compartments. A dose lands in one by its scheduled time,
# or in Bedtime when its plan label says "bedtime".
PERIODS: list[tuple[str, str, time]] = [
    ("morning", "Morning", time(4, 0)),
    ("afternoon", "Afternoon", time(12, 0)),
    ("evening", "Evening", time(17, 0)),
    ("bedtime", "Bedtime", time(20, 30)),
]
PERIOD_NAMES = {key: name for key, name, _ in PERIODS}


def period_for(hhmm: str | None, label: str | None = None) -> str:
    if label and "bed" in label.lower():
        return "bedtime"
    if not hhmm:
        return "morning"
    t = time.fromisoformat(hhmm)
    current = "bedtime"  # before 04:00 counts as the night before
    for key, _, start in PERIODS:
        if t >= start:
            current = key
    return current


# ---------------------------------------------------------------- real elapsed time
#
# Times are stored as local wall-clock strings. When the clocks change, wall-clock
# arithmetic is off by an hour: on the spring-forward night "6 hours after 11 PM" is
# 6 AM, not 5 AM. Gaps between doses use real elapsed time so the app never says
# a medicine is OK an hour early.

_TZ: ZoneInfo | None = None


def set_timezone(tz: ZoneInfo | None) -> None:
    global _TZ
    _TZ = tz


def _utc(local: datetime) -> datetime:
    assert _TZ is not None
    return local.replace(tzinfo=_TZ).astimezone(UTC)


def real_add(local: datetime, delta: timedelta) -> datetime:
    """Wall-clock time `delta` of real time after `local`."""
    if _TZ is None:
        return local + delta
    return (_utc(local) + delta).astimezone(_TZ).replace(tzinfo=None)


def real_diff(later: datetime, earlier: datetime) -> timedelta:
    """Real time between two wall-clock times."""
    if _TZ is None:
        return later - earlier
    return _utc(later) - _utc(earlier)


# ---------------------------------------------------------------- formatting


def parse_local(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("T", " ")[:16])


def local_from_utc(iso: str) -> datetime:
    """A stored UTC timestamp (like a backup's) as local wall-clock time."""
    return datetime.fromisoformat(iso).astimezone(_TZ).replace(tzinfo=None)


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_time(value: str | time | datetime | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        value = time.fromisoformat(value[-5:])
    if isinstance(value, datetime):
        value = value.time()
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def fmt_day(d: date) -> str:
    return f"{d:%a}, {d:%b} {d.day}"


def fmt_relative_day(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    return fmt_day(d)


def fmt_duration(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 1:
        return "less than a minute"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    if hours:
        return f"{hours} hr"
    return f"{mins} min"


def day_label(day: int) -> str:
    if day == 0:
        return "Day 0 · day before chemo"
    if day == 1:
        return "Day 1 · chemo day"
    return f"Day {day}"


def days_text(start: int, end: int | None) -> str:
    if end is None:
        return f"Day {start} to end of cycle" if start > 1 else "Every day of the cycle"
    if start == end:
        return f"Day {start}"
    if end - start == 1:
        return f"Days {start} and {end}"
    return f"Days {start}–{end}"


# ---------------------------------------------------------------- cycles


def all_cycles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cycles ORDER BY day1, number").fetchall()


def day1_of(cycle: sqlite3.Row) -> date:
    return date.fromisoformat(cycle["day1"])


def day_number(cycle: sqlite3.Row, d: date) -> int:
    return (d - day1_of(cycle)).days + 1


def date_of(cycle: sqlite3.Row, day: int) -> date:
    return day1_of(cycle) + timedelta(days=day - 1)


def cycle_for_date(conn: sqlite3.Connection, d: date) -> sqlite3.Row | None:
    """The latest cycle whose Day 0 is on or before d."""
    found = None
    for c in all_cycles(conn):
        if d >= day1_of(c) - timedelta(days=1):
            found = c
    return found


def next_cycle_after(conn: sqlite3.Connection, d: date) -> sqlite3.Row | None:
    return next((c for c in all_cycles(conn) if day1_of(c) - timedelta(days=1) > d), None)


# ---------------------------------------------------------------- plans

PLAN_SQL = """
SELECT p.*, m.name AS med_name, m.brand AS med_brand, m.info_key AS med_info, m.route AS med_route,
       m.strength AS med_strength, m.dose AS med_dose, m.directions AS med_directions,
       m.min_hours_between AS med_gap, m.max_per_day AS med_max,
       m.prep_minutes AS med_prep, m.prep_note AS med_prep_note
  FROM plans p JOIN medications m ON m.id = p.medication_id
 WHERE m.active = 1 AND p.from_cycle <= :n AND (p.to_cycle IS NULL OR p.to_cycle >= :n)
 ORDER BY CASE p.kind WHEN 'clinic' THEN 0 WHEN 'scheduled' THEN 1 ELSE 2 END, m.name, p.id
"""


def plans_for(conn: sqlite3.Connection, cycle: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute(PLAN_SQL, {"n": cycle["number"]}).fetchall()


def plan_end(plan: sqlite3.Row, cycle: sqlite3.Row) -> int:
    return plan["end_day"] if plan["end_day"] is not None else int(cycle["length_days"])


def plan_times(plan: sqlite3.Row) -> list[str]:
    return [t for t in (plan["times"] or "").split(",") if t]


def cycle_span(conn: sqlite3.Connection, cycle: sqlite3.Row) -> int:
    last = max((plan_end(p, cycle) for p in plans_for(conn, cycle)), default=0)
    return max(int(cycle["length_days"]), last)


# ---------------------------------------------------------------- slots


@dataclass
class Slot:
    plan: sqlite3.Row
    cycle: sqlite3.Row
    day: int
    date: date
    time: str | None
    log: sqlite3.Row | None = None

    @property
    def key(self) -> str:
        return f"{self.date.isoformat()} {self.time}" if self.time else self.date.isoformat()

    @property
    def dt(self) -> datetime:
        return datetime.combine(self.date, time.fromisoformat(self.time) if self.time else time(0, 0))

    @property
    def kind(self) -> str:
        return self.plan["kind"]

    @property
    def period(self) -> str:
        return period_for(self.time, self.plan["label"])

    @property
    def dose(self) -> str:
        return self.plan["dose"] or self.plan["med_dose"] or ""

    @property
    def prep_at(self) -> datetime | None:
        """When to start getting this dose ready (e.g. out of the fridge), for home doses at a set time."""
        if self.kind != "scheduled" or not self.time or not self.plan["med_prep"]:
            return None
        return real_add(self.dt, -timedelta(minutes=self.plan["med_prep"]))

    def prep_text(self, now: datetime) -> str:
        """'Take it out of the fridge at 1:00 PM', or '... now' once it's time. Empty when done or not needed."""
        at = self.prep_at
        if at is None or self.log is not None or now >= self.dt + DUE_AFTER:
            return ""
        note = self.plan["med_prep_note"] or "Get it ready"
        if now >= at:
            return f"{note} now"
        when = fmt_time(at) if at.date() == now.date() else f"{fmt_relative_day(at.date(), now.date()).lower()} at {fmt_time(at)}"
        return f"{note} at {when}"

    def status(self, now: datetime) -> str:
        if self.log is not None:
            return self.log["status"]
        if self.kind == "clinic":
            return "clinic" if self.date >= now.date() else "not_logged"
        if self.date > now.date() or now < self.dt - DUE_BEFORE:
            return "upcoming"
        if now <= self.dt + DUE_AFTER:
            return "due"
        return "late" if self.date == now.date() else "missed"

    def status_text(self, now: datetime) -> str:
        s = self.status(now)
        if s == "taken":
            when = parse_local(self.log["taken_at"]) if self.log else None
            word = "Given" if self.kind == "clinic" else "Taken"
            return f"{word} {fmt_time(when)}" if when else word
        if s == "skipped":
            return "Skipped"
        if s == "clinic":
            return "At the clinic"
        if s == "not_logged":
            return "Not logged"
        if s == "due":
            return "Due now"
        if s == "late":
            return f"Was due {fmt_time(self.time)}"
        if s == "missed":
            return "Not logged"
        if self.date == now.date():
            if self.period == "bedtime":
                return "Tonight"
            return f"In {fmt_duration(self.dt - now)}"
        return "Planned"


LOG_SQL = """
SELECT dl.*, m.name AS med_name, who.name AS who
  FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
  LEFT JOIN profiles who ON who.id = COALESCE(dl.updated_by, dl.logged_by)
 WHERE COALESCE(dl.slot, dl.taken_at) >= ? AND COALESCE(dl.slot, dl.taken_at) < ?
 ORDER BY COALESCE(dl.taken_at, dl.slot), dl.id
"""


def _log_days(log: sqlite3.Row) -> list[date]:
    """The day a logged dose belongs to: the planned day it was logged for, else the day it was taken.

    A dose taken in the small hours without a planned slot may also be last night's evening dose.
    """
    if log["slot"]:
        return [date.fromisoformat(log["slot"][:10])]
    taken = parse_local(log["taken_at"])
    if taken is None:
        return []
    return [taken.date(), taken.date() - timedelta(days=1)] if taken.hour < 5 else [taken.date()]


def _log_time(log: sqlite3.Row) -> datetime | None:
    return parse_local(log["taken_at"]) or (parse_local(log["slot"]) if log["slot"] and len(log["slot"]) > 10 else None)


def _pair_logs(out: list[Slot], logs: list[sqlite3.Row], plan_kinds: dict[int, str],
               as_needed_days: dict[int, set[date]]) -> list[sqlite3.Row]:
    """Attach each logged dose to its planned slot. Returns the logged doses that fit no slot.

    A dose is tied to its slot by plan and planned time, but a dose that was logged still counts when the
    schedule changes afterwards: a new time, new days, or a replaced plan. Otherwise the row shows as due
    again and the family could give a second dose. Those doses, and doses logged from "Log a dose", go to
    the nearest open slot of the same medicine on the same day. An unplanned dose of a medicine that is
    also allowed as needed that day is left alone, because it may be the as-needed one.
    """
    used: set[int] = set()
    by_key = {(log["plan_id"], log["slot"]): log for log in logs if log["plan_id"] is not None and log["slot"]}
    for s in out:
        log = by_key.get((s.plan["id"], s.key))
        if log is not None:
            s.log = log
            used.add(log["id"])
    open_slots: dict[tuple[int, date], list[Slot]] = {}
    for s in out:
        if s.log is None:
            open_slots.setdefault((s.plan["medication_id"], s.date), []).append(s)
    pairs: list[tuple[int, int, float, int, Slot]] = []
    candidates: list[sqlite3.Row] = []
    for log in logs:
        if log["id"] in used or log["status"] not in ("taken", "skipped"):
            continue
        if plan_kinds.get(log["plan_id"]) == "as_needed":
            continue  # "Took one" from the as-needed shelf, which shows it
        days = _log_days(log)
        if not log["slot"] and days and days[0] in as_needed_days.get(log["medication_id"], set()):
            continue  # may be the as-needed dose: don't guess
        candidates.append(log)
        at = _log_time(log)
        for i, d in enumerate(days):
            for s in open_slots.get((log["medication_id"], d), []):
                if i == 1 and (not s.time or s.time < "18:00"):
                    continue  # the small hours only belong to last night's evening dose
                gap = abs(real_diff(at, s.dt).total_seconds()) if (at is not None and s.time) else 0.0
                pairs.append((0 if log["plan_id"] == s.plan["id"] else 1, i, gap, log["id"], s))
    logs_by_id = {log["id"]: log for log in logs}
    for _, _, _, log_id, s in sorted(pairs, key=lambda p: p[:3]):
        if log_id not in used and s.log is None:
            s.log = logs_by_id[log_id]
            used.add(log_id)
    return [log for log in candidates if log["id"] not in used]


def _slots_and_extras(conn: sqlite3.Connection, cycle: sqlite3.Row) -> tuple[list[Slot], list[sqlite3.Row]]:
    span = cycle_span(conn, cycle)
    plans = plans_for(conn, cycle)
    out: list[Slot] = []
    as_needed_days: dict[int, set[date]] = {}
    for plan in plans:
        days = range(plan["start_day"], plan_end(plan, cycle) + 1)
        if plan["kind"] == "as_needed":
            as_needed_days.setdefault(plan["medication_id"], set()).update(date_of(cycle, dn) for dn in days)
            continue
        for dn in days:
            d = date_of(cycle, dn)
            for t in plan_times(plan) if plan["kind"] == "scheduled" else [None]:
                out.append(Slot(plan=plan, cycle=cycle, day=dn, date=d, time=t))
    start, end = date_of(cycle, 0), date_of(cycle, span) + timedelta(days=2)
    logs = conn.execute(LOG_SQL, (start.isoformat(), end.isoformat())).fetchall()
    plan_kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM plans")}
    extras = _pair_logs(out, logs, plan_kinds, as_needed_days)
    out.sort(key=lambda s: (s.date, s.kind != "clinic", s.time or "", s.plan["med_name"]))
    return out, extras


def slots(conn: sqlite3.Connection, cycle: sqlite3.Row, day: int | None = None) -> list[Slot]:
    """Every planned clinic/scheduled dose in a cycle (or one day of it), each with the dose logged for it.

    Logged doses are paired across the whole cycle even when only one day is wanted, so a day always shows
    the same thing whichever way it's looked at.
    """
    out, _ = _slots_and_extras(conn, cycle)
    return out if day is None else [s for s in out if s.day == day]


def extra_doses(conn: sqlite3.Connection, cycle: sqlite3.Row | None, d: date) -> list[dict]:
    """Doses logged on `d` that fit no planned slot, so Today can show them instead of hiding them.

    `note` says why it matters: more doses than planned, or a planned medicine on a day it isn't planned.
    """
    if cycle is None:
        return []
    out, extras = _slots_and_extras(conn, cycle)
    planned_today = {s.plan["medication_id"] for s in out if s.date == d}
    planned_ever = {s.plan["medication_id"] for s in out}
    rows = []
    for log in extras:
        if _log_days(log)[:1] != [d]:
            continue
        mid = log["medication_id"]
        note = ("More than planned. Check it isn't the same dose logged twice." if mid in planned_today
                else "Not on the schedule this day." if mid in planned_ever else "")
        rows.append({**dict(log), "note": note})
    return rows


def find_slot(conn: sqlite3.Connection, plan_id: int, key: str) -> Slot | None:
    d = date.fromisoformat(key[:10])
    cycle = cycle_for_date(conn, d)
    if cycle is None:
        return None
    dn = day_number(cycle, d)
    return next((s for s in slots(conn, cycle, dn) if s.plan["id"] == plan_id and s.key == key), None)


# ---------------------------------------------------------------- as-needed


@dataclass
class AsNeeded:
    medication_id: int
    name: str
    brand: str | None
    strength: str | None
    dose: str | None
    info_key: str | None
    plan: sqlite3.Row | None
    last: sqlite3.Row | None
    count_today: int
    gap_hours: float | None
    max_per_day: int | None
    scheduled_today: bool = True
    schedule_note: str = ""

    @property
    def last_at(self) -> datetime | None:
        return parse_local(self.last["taken_at"]) if self.last else None

    @property
    def next_allowed(self) -> datetime | None:
        if self.last_at and self.gap_hours:
            return real_add(self.last_at, timedelta(hours=self.gap_hours))
        return None

    def at_max(self) -> bool:
        return self.max_per_day is not None and self.count_today >= self.max_per_day

    @property
    def gap_known(self) -> bool:
        return bool(self.gap_hours)

    def available(self, now: datetime) -> bool:
        if self.at_max() or not self.scheduled_today:
            return False
        return self.next_allowed is None or now >= self.next_allowed

    def state(self, now: datetime) -> str:
        """off (not planned today), max (daily limit reached), wait, ok, or open.

        open means no wait time is saved, so the app can't say when the next one is OK.
        It must never look like "OK to take now".
        """
        if not self.scheduled_today:
            return "off"
        if self.at_max():
            return "max"
        if not self.available(now):
            return "wait"
        return "ok" if self.gap_known else "open"

    def fraction(self, now: datetime) -> float:
        """How far through the waiting gap we are (1.0 = OK now, 0.0 when the gap isn't known)."""
        gap = self.gap_hours
        if self.at_max() or not self.scheduled_today or not gap:
            return 0.0
        if not self.last_at or self.available(now):
            return 1.0
        total = gap * 3600
        return min(max(real_diff(now, self.last_at).total_seconds() / total, 0.0), 1.0)

    def status_text(self, now: datetime) -> str:
        if not self.scheduled_today:
            return f"Not planned today ({self.schedule_note})" if self.schedule_note else "Not planned today"
        if self.at_max():
            return f"Reached {self.max_per_day} today"
        if self.available(now):
            return "OK to take now" if self.gap_known else "Time between doses not saved"
        nxt = self.next_allowed
        assert nxt is not None
        prefix = "OK at" if nxt.date() == now.date() else f"OK {fmt_relative_day(nxt.date(), now.date()).lower()} at"
        return f"{prefix} {fmt_time(nxt)}"

    def wait_text(self, now: datetime) -> str:
        if self.state(now) == "open":
            return "Follow the label"
        nxt = self.next_allowed
        if nxt is None or self.available(now) or not self.scheduled_today:
            return ""
        return f"in {fmt_duration(real_diff(nxt, now))}"


# Two people giving the same medicine minutes apart is the classic double dose.
RECENT_DUPLICATE = timedelta(minutes=30)


def early_dose_warning(conn: sqlite3.Connection, med: sqlite3.Row, when: datetime, by: int | None = None) -> str | None:
    """A plain sentence if logging a dose at `when` looks like a double dose, or breaks the label's gap or daily limit."""
    gap, most = med["min_hours_between"], med["max_per_day"]
    limit = timedelta(hours=gap) if gap else RECENT_DUPLICATE
    window = limit + timedelta(hours=1)  # wall-clock search; the real gap is checked below
    near = conn.execute(
        """SELECT dl.taken_at, dl.logged_by, p.name AS who FROM dose_logs dl LEFT JOIN profiles p ON p.id = dl.logged_by
            WHERE dl.medication_id = ? AND dl.status = 'taken' AND dl.taken_at > ? AND dl.taken_at < ?
            ORDER BY abs(julianday(dl.taken_at) - julianday(?)) LIMIT 1""",
        (med["id"], fmt_local(when - window), fmt_local(when + window), fmt_local(when)),
    ).fetchone()
    if near:
        other = parse_local(near["taken_at"])
        assert other is not None
        apart = abs(real_diff(when, other))
        if apart < limit:
            if by is not None and near["logged_by"] == by:
                who = "You logged"
            elif near["who"]:
                who = f"{near['who']} logged"
            else:
                who = "There's"
            when_txt = f"{fmt_relative_day(other.date(), when.date()).lower()} at {fmt_time(other)}" \
                if other.date() != when.date() else f"at {fmt_time(other)}"
            side = "before this one" if other <= when else "after this time"
            first = f"{who} one {when_txt}, {fmt_duration(apart)} {side}."
            if gap:
                return f"{first} The label says at least {gap:g} hours between doses."
            return f"{first} Make sure it isn't the same dose logged twice."
    if most:
        count = conn.execute(
            """SELECT COUNT(*) FROM dose_logs WHERE medication_id = ? AND status = 'taken' AND substr(taken_at, 1, 10) = ?""",
            (med["id"], when.date().isoformat()),
        ).fetchone()[0]
        if count >= most:
            return f"{count} {'dose is' if count == 1 else 'doses are'} already logged that day. The label allows up to {most}."
    return None


def _who_logged(log: sqlite3.Row, by: int | None) -> str:
    if by is not None and (log["updated_by"] or log["logged_by"]) == by:
        return "you"
    return log["who"] if "who" in log.keys() and log["who"] else "someone"


def _days_text(plans: list[sqlite3.Row], cycle: sqlite3.Row) -> str:
    spans = []
    for p in plans:
        a, b = p["start_day"], plan_end(p, cycle)
        spans.append(f"Day {a}" if a == b else f"Days {a}–{b}")
    return ", ".join(dict.fromkeys(spans))


def dose_warning(conn: sqlite3.Connection, med: sqlite3.Row, when: datetime, by: int | None = None,
                 slot: Slot | None = None, now: datetime | None = None, unplanned_ok: bool = False) -> str | None:
    """A plain sentence when logging this dose could be a double dose, or None.

    Every way of logging a dose asks this first: a scheduled row's Taken or Given, Took all, Log a dose and
    Took one. As-needed medicines keep the label's gap and daily limit (early_dose_warning). Planned
    medicines are also compared with that day's schedule: all of the day's doses already logged, another
    dose less than half the planned gap away, or a scheduled-only medicine on a day it isn't planned.
    """
    warn = early_dose_warning(conn, med, when, by)
    if warn:
        return warn
    d = slot.date if slot is not None else when.date()
    cycle = cycle_for_date(conn, d)
    if cycle is None:
        return None
    dn = day_number(cycle, d)
    plans = [p for p in plans_for(conn, cycle) if p["medication_id"] == med["id"]]
    if any(p["kind"] == "as_needed" and p["start_day"] <= dn <= plan_end(p, cycle) for p in plans):
        return None  # allowed as needed today: the gap and daily limit above are the rule
    planned_plans = [p for p in plans if p["kind"] != "as_needed"]
    if not planned_plans:
        return None  # not a planned medicine in this cycle
    out, extras = _slots_and_extras(conn, cycle)
    mine = [s for s in out if s.plan["medication_id"] == med["id"] and s.date == d]
    today = (now or when).date()
    day_word = "today" if d == today else ("yesterday" if (today - d).days == 1 else f"on {fmt_day(d)}")
    if not mine:
        if unplanned_ok:
            return None
        return (f"{med['name']} isn't planned {day_word}. Your schedule has it on {_days_text(planned_plans, cycle)}. "
                "Make sure it wasn't already given and that the care team wants this dose.")
    taken = [s.log for s in mine if s.log is not None and s.log["status"] == "taken"
             and not (slot is not None and s.plan["id"] == slot.plan["id"] and s.key == slot.key)]
    taken += [log for log in extras if log["medication_id"] == med["id"] and log["status"] == "taken"
              and _log_days(log)[:1] == [d]]
    if taken and len(taken) >= len(mine):
        last = max(taken, key=lambda log: log["taken_at"] or "")
        who = _who_logged(last, by)
        at = f" at {fmt_time(last['taken_at'])}" if last["taken_at"] else ""
        if len(mine) == 1:
            return f"{med['name']} is planned once {day_word}, and {who} already logged it{at}. Make sure it isn't the same dose logged twice."
        return (f"{med['name']} is planned {len(mine)} times {day_word}, and all {len(mine)} are already logged, "
                f"the last{at}. Make sure it isn't the same dose logged twice.")
    times = sorted(s.dt for s in mine if s.time)
    if times:
        gap = min((b - a for a, b in zip(times, times[1:])), default=timedelta(hours=24))
        near = conn.execute(
            """SELECT dl.*, p.name AS who FROM dose_logs dl LEFT JOIN profiles p ON p.id = COALESCE(dl.updated_by, dl.logged_by)
                WHERE dl.medication_id = ? AND dl.status = 'taken' AND dl.taken_at > ? AND dl.taken_at < ?
                ORDER BY abs(julianday(dl.taken_at) - julianday(?)) LIMIT 1""",
            (med["id"], fmt_local(when - gap), fmt_local(when + gap), fmt_local(when)),
        ).fetchone()
        if near is not None:
            other = parse_local(near["taken_at"])
            assert other is not None
            apart = abs(real_diff(when, other))
            if apart < gap / 2:
                side = "before this one" if other <= when else "after this time"
                other_day = "" if other.date() == when.date() else f"{fmt_relative_day(other.date(), when.date()).lower()} "
                every = "once a day" if len(times) == 1 else f"{fmt_duration(gap)} apart"
                return (f"{_who_logged(near, by).capitalize()} logged {med['name']} {other_day}at {fmt_time(other)}, "
                        f"{fmt_duration(apart)} {side}. It's planned {every}.")
    return None


def last_taken(conn: sqlite3.Connection, medication_id: int, before: datetime) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM dose_logs WHERE medication_id = ? AND status = 'taken' AND taken_at IS NOT NULL
             AND taken_at <= ? ORDER BY taken_at DESC LIMIT 1""",
        (medication_id, fmt_local(before)),
    ).fetchone()


def _as_needed_row(conn: sqlite3.Connection, med: sqlite3.Row, plan: sqlite3.Row | None, now: datetime) -> AsNeeded:
    count = conn.execute(
        """SELECT COUNT(*) FROM dose_logs WHERE medication_id = ? AND status = 'taken'
             AND substr(taken_at, 1, 10) = ?""",
        (med["id"], now.date().isoformat()),
    ).fetchone()[0]
    return AsNeeded(
        medication_id=med["id"],
        name=med["name"],
        brand=med["brand"],
        strength=med["strength"],
        dose=med["dose"],
        info_key=med["info_key"],
        plan=plan,
        last=last_taken(conn, med["id"], now),
        count_today=count,
        gap_hours=med["min_hours_between"],
        max_per_day=med["max_per_day"],
    )


def as_needed_for_day(conn: sqlite3.Connection, cycle: sqlite3.Row | None, d: date, now: datetime) -> list[AsNeeded]:
    if cycle is None:
        return []
    dn = day_number(cycle, d)
    out, seen = [], set()
    for plan in plans_for(conn, cycle):
        if plan["kind"] != "as_needed" or plan["medication_id"] in seen:
            continue
        if not (plan["start_day"] <= dn <= plan_end(plan, cycle)):
            continue
        med = conn.execute("SELECT * FROM medications WHERE id = ?", (plan["medication_id"],)).fetchone()
        seen.add(plan["medication_id"])
        out.append(_as_needed_row(conn, med, plan, now))
    return out


def as_needed_for_symptom(conn: sqlite3.Connection, symptom: str, now: datetime) -> list[AsNeeded]:
    """The patient's own as-needed medicines commonly used for this symptom."""
    keys = sx.TREATED_BY.get(symptom, ())
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    meds = conn.execute(
        f"""SELECT m.* FROM medications m
             WHERE m.active = 1 AND m.info_key IN ({marks})
               AND EXISTS (SELECT 1 FROM plans p WHERE p.medication_id = m.id AND p.kind = 'as_needed')
             ORDER BY m.name""",
        keys,
    ).fetchall()
    cycle = cycle_for_date(conn, now.date())
    dn = day_number(cycle, now.date()) if cycle else None
    out = []
    for m in meds:
        row = _as_needed_row(conn, m, None, now)
        prn_plans = [p for p in (plans_for(conn, cycle) if cycle else []) if p["medication_id"] == m["id"] and p["kind"] == "as_needed"]
        covering = [p for p in prn_plans if dn is not None and p["start_day"] <= dn <= plan_end(p, cycle)]
        if covering:
            row.plan = covering[0]
        else:
            # Respect the clinic's day limits (e.g. "Days 4-6 only").
            row.scheduled_today = False
            row.schedule_note = " and ".join(days_text(p["start_day"], p["end_day"]) for p in prn_plans)
        out.append(row)
    out.sort(key=lambda a: (not a.scheduled_today, a.name))
    return out


# ---------------------------------------------------------------- today


@dataclass
class Compartment:
    key: str
    name: str
    slots: list[Slot]

    def all_done(self) -> bool:
        return bool(self.slots) and all(s.log is not None for s in self.slots)

    def state(self, now: datetime) -> str:
        """done, due (now, or the clinic dose on its day), late (today, past its window), upcoming, or missed."""
        if self.all_done():
            return "done"
        open_ = {s.status(now) for s in self.slots if s.log is None}
        if "due" in open_ or "clinic" in open_:
            return "due"
        if "late" in open_:
            return "late"
        if "upcoming" in open_:
            return "upcoming"
        return "missed"

    def first_open(self) -> Slot | None:
        return next((s for s in self.slots if s.log is None), None)

    def open_now(self, now: datetime) -> list[Slot]:
        """Doses here someone could log now: home doses that are due or past and not logged yet.

        "Took all" covers only these. Not-yet-due doses stay out, so nobody marks a 10 AM pill taken at 8.
        Clinic doses stay out too: the infusion team gives them one by one.
        """
        return [s for s in self.slots if s.log is None and s.kind != "clinic"
                and s.status(now) in ("due", "late", "missed", "not_logged")]

    def lid_text(self, now: datetime) -> str:
        """The short line on the organizer lid: Done, Due now, Was due 8:00 AM, 9:30 PM, Not logged."""
        st = self.state(now)
        if st == "done":
            return "Done"
        if st == "due":
            return "At the clinic" if self.key == "clinic" else "Due now"
        nxt = self.first_open()
        if st == "late" and nxt is not None and nxt.time:
            return f"Was due {fmt_time(nxt.time)}"
        if st == "upcoming" and nxt is not None:
            return fmt_time(nxt.time) if nxt.time else "Planned"
        return "Not logged"

    def lid_short(self, now: datetime) -> str:
        """The same, short enough for a lid a quarter of a phone wide."""
        st = self.state(now)
        nxt = self.first_open()
        if st == "upcoming" and nxt is not None and nxt.time:
            return fmt_time(nxt.time)
        return {"done": "Done", "due": "Due now", "late": "Late", "upcoming": "Planned"}.get(st, "Missed")


@dataclass
class DayPlan:
    cycle: sqlite3.Row | None
    day: int | None
    date: date
    clinic: list[Slot]
    compartments: list[Compartment]
    as_needed: list[AsNeeded]

    @property
    def scheduled(self) -> list[Slot]:
        return [s for c in self.compartments for s in c.slots]

    def counts(self) -> tuple[int, int]:
        planned = self.scheduled + self.clinic
        return sum(1 for s in planned if s.log is not None), len(planned)

    def all_compartments(self) -> list[Compartment]:
        clinic = [Compartment("clinic", "At the clinic", self.clinic)] if self.clinic else []
        return clinic + self.compartments

    def lids(self) -> list[tuple[str, str, Compartment | None]]:
        """The pill-organizer row: every time of day, empty ones included, plus the clinic on its day."""
        by_key = {c.key: c for c in self.all_compartments()}
        keys = (["clinic"] if "clinic" in by_key else []) + [k for k, _, _ in PERIODS]
        names = {"clinic": "Clinic", **PERIOD_NAMES}
        return [(k, names[k], by_key.get(k)) for k in keys]

    def sections(self, now: datetime) -> list[tuple[str, Compartment | None]]:
        """What Today lists, in order. On today: anything due first, then the as-needed shelf, then the rest
        by time of day. Other days read top to bottom like the organizer."""
        comps = self.all_compartments()
        prn: list[tuple[str, Compartment | None]] = [("prn", None)] if self.as_needed else []
        if self.date != now.date():
            return [("comp", c) for c in comps] + prn
        urgent = [c for c in comps if c.state(now) in ("due", "late")]
        rest = [c for c in comps if c.state(now) not in ("due", "late")]
        return [("comp", c) for c in urgent] + prn + [("comp", c) for c in rest]

    def as_needed_by_readiness(self, now: datetime) -> list[AsNeeded]:
        order = {"ok": 0, "open": 1, "wait": 2, "max": 3, "off": 4}
        return sorted(self.as_needed, key=lambda a: (order[a.state(now)], a.next_allowed or now, a.name))


def day_plan(conn: sqlite3.Connection, d: date, now: datetime) -> DayPlan:
    cycle = cycle_for_date(conn, d)
    if cycle is None:
        return DayPlan(None, None, d, [], [], [])
    dn = day_number(cycle, d)
    day_slots = slots(conn, cycle, dn)
    groups: dict[str, list[Slot]] = {key: [] for key, _, _ in PERIODS}
    for s in day_slots:
        if s.kind == "scheduled":
            groups[s.period].append(s)
    return DayPlan(
        cycle=cycle,
        day=dn,
        date=d,
        clinic=[s for s in day_slots if s.kind == "clinic"],
        compartments=[Compartment(k, PERIOD_NAMES[k], v) for k, v in groups.items() if v],
        as_needed=as_needed_for_day(conn, cycle, d, now),
    )


def next_doses(conn: sqlite3.Connection, now: datetime, limit: int = 3) -> list[Slot]:
    """Unlogged scheduled doses from 'due now' onward, across the current and next cycle."""
    found: list[Slot] = []
    candidates = [c for c in (cycle_for_date(conn, now.date()), next_cycle_after(conn, now.date())) if c]
    for c in candidates:
        for s in slots(conn, c):
            if s.kind == "scheduled" and s.log is None and s.dt >= now - DUE_AFTER:
                found.append(s)
    found.sort(key=lambda s: s.dt)
    return found[:limit]


def asks_if_it_helped(conn: sqlite3.Connection, log: sqlite3.Row) -> bool:
    """Only as-needed medicine is taken to relieve something, so only it gets "Did it help?".

    Scheduled prevention (anti-nausea, steroids), white-cell boosters, cancer pills and clinic doses can't be
    judged from one dose. Doses logged at another time are stored as kind 'as_needed' whatever the medicine
    is, so look at the plan: the dose's own plan if it has one, otherwise whether the medicine has an
    as-needed plan at all.
    """
    if log["kind"] == "clinic":
        return False
    if log["plan_id"]:
        row = conn.execute("SELECT kind FROM plans WHERE id = ?", (log["plan_id"],)).fetchone()
        if row is not None:
            return row["kind"] == "as_needed"
    return conn.execute("SELECT 1 FROM plans WHERE medication_id = ? AND kind = 'as_needed' LIMIT 1",
                        (log["medication_id"],)).fetchone() is not None


def follow_ups(conn: sqlite3.Connection, now: datetime, limit: int = 3) -> list[sqlite3.Row]:
    """Recent doses still missing 'did it help / how was the dose'."""
    return conn.execute(
        """SELECT dl.*, m.name AS med_name, m.brand AS med_brand, m.info_key AS med_info
             FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
            WHERE dl.status = 'taken' AND dl.kind = 'as_needed' AND dl.helped IS NULL
              AND EXISTS (SELECT 1 FROM plans p WHERE p.medication_id = dl.medication_id AND p.kind = 'as_needed'
                                                AND (dl.plan_id IS NULL OR p.id = dl.plan_id))
              AND dl.taken_at BETWEEN ? AND ?
            ORDER BY dl.taken_at DESC LIMIT ?""",
        (fmt_local(now - FOLLOW_UP_WITHIN), fmt_local(now - FOLLOW_UP_AFTER), limit),
    ).fetchall()


# ---------------------------------------------------------------- symptoms


def symptoms_between(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM symptom_logs WHERE logged_at >= ? AND logged_at < ? ORDER BY logged_at",
        (start.isoformat(), (end + timedelta(days=1)).isoformat()),
    ).fetchall()


def symptom_score(row: sqlite3.Row, fever_f: float = 100.4) -> int:
    """Map any symptom entry onto 0-3 so days can be compared at a glance."""
    sym = sx.lookup(row["symptom"])
    value = row["value"]
    if sym.kind == "temp":
        return 3 if value is not None and value >= fever_f else (1 if value is not None and value >= fever_f - 1 else 0)
    if sym.kind == "pain":
        return 0 if not value else (1 if value <= 3 else 2 if value <= 6 else 3)
    if sym.kind == "count":
        return 0 if not value else (1 if value <= 1 else 2 if value <= 4 else 3)
    return row["severity"] or 0


# ---------------------------------------------------------------- ring


@dataclass
class RingDay:
    day: int
    date: date
    planned: int
    done: int
    as_needed: int
    worst: int
    is_today: bool
    is_chemo: bool
    is_past: bool = False
    symptoms: list[tuple[str, int]] = field(default_factory=list)  # (name, worst score that day), worst first

    @property
    def state(self) -> str:
        if self.planned == 0:
            return "rest"
        if self.done >= self.planned:
            return "done"
        return "partial" if self.done else "planned"

    @property
    def mark(self) -> str:
        """How the dial draws this day. A past day with planned doses left unlogged is a gap, shown openly."""
        if self.is_past and self.state in ("planned", "partial"):
            return "gap"
        return self.state


def ring(conn: sqlite3.Connection, cycle: sqlite3.Row, today: date, fever_f: float = 100.4) -> list[RingDay]:
    span = cycle_span(conn, cycle)
    all_slots = slots(conn, cycle)
    start, end = date_of(cycle, 0), date_of(cycle, span)
    prn = {
        r["d"]: r["n"]
        for r in conn.execute(
            """SELECT substr(taken_at, 1, 10) AS d, COUNT(*) AS n FROM dose_logs
                WHERE kind = 'as_needed' AND status = 'taken' AND taken_at >= ? AND taken_at < ? GROUP BY d""",
            (start.isoformat(), (end + timedelta(days=1)).isoformat()),
        )
    }
    worst: dict[str, int] = {}
    named: dict[str, dict[str, int]] = {}
    for row in symptoms_between(conn, start, end):
        k = row["logged_at"][:10]
        score = symptom_score(row, fever_f)
        worst[k] = max(worst.get(k, 0), score)
        name = sx.lookup(row["symptom"]).name if row["symptom"] != "other" else (row["label"] or "Other")
        named.setdefault(k, {})[name] = max(named.get(k, {}).get(name, 0), score)
    out = []
    for dn in range(0, span + 1):
        d = date_of(cycle, dn)
        day_slots = [s for s in all_slots if s.day == dn]
        out.append(
            RingDay(
                day=dn,
                date=d,
                planned=len(day_slots),
                done=sum(1 for s in day_slots if s.log is not None),
                as_needed=prn.get(d.isoformat(), 0),
                worst=worst.get(d.isoformat(), 0),
                is_today=d == today,
                is_chemo=dn == 1,
                is_past=d < today,
                symptoms=sorted(named.get(d.isoformat(), {}).items(), key=lambda kv: (-kv[1], kv[0])),
            )
        )
    return out


# ---------------------------------------------------------------- grid


@dataclass
class GridCell:
    allowed: bool = False  # as-needed medicine is on the schedule this day
    planned: int = 0
    taken: list[sqlite3.Row] = field(default_factory=list)
    skipped: list[sqlite3.Row] = field(default_factory=list)
    missing: int = 0
    upcoming: int = 0
    times: list[str] = field(default_factory=list)  # planned clock times this day


@dataclass
class GridRow:
    medication_id: int
    name: str
    kind: str
    detail: str
    cells: dict[int, GridCell]


@dataclass
class SymptomRow:
    symptom: str
    name: str
    cells: dict[int, list[sqlite3.Row]]


def grid(conn: sqlite3.Connection, cycle: sqlite3.Row, now: datetime) -> tuple[list[int], list[GridRow], list[SymptomRow]]:
    """Paper-sheet style grid: one row per (medicine, kind), one column per cycle day."""
    span = cycle_span(conn, cycle)
    days = list(range(0, span + 1))
    rows: dict[tuple[int, str], GridRow] = {}
    for plan in plans_for(conn, cycle):
        key = (plan["medication_id"], plan["kind"])
        if key not in rows:
            detail = " · ".join(x for x in (plan["med_strength"], plan["dose"] or plan["med_dose"]) if x)
            rows[key] = GridRow(plan["medication_id"], plan["med_name"], plan["kind"], detail, {d: GridCell() for d in days})
        if plan["kind"] == "as_needed":
            for dn in range(plan["start_day"], plan_end(plan, cycle) + 1):
                if dn in rows[key].cells:
                    rows[key].cells[dn].allowed = True
    for s in slots(conn, cycle):
        cell = rows[(s.plan["medication_id"], s.kind)].cells[s.day]
        cell.planned += 1
        if s.time:
            cell.times.append(s.time)
        if s.log is None:
            if s.status(now) in ("missed", "late", "not_logged"):
                cell.missing += 1
            else:
                cell.upcoming += 1
    start, end = date_of(cycle, 0), date_of(cycle, span)
    logs = conn.execute(
        """SELECT * FROM dose_logs WHERE COALESCE(taken_at, slot) >= ? AND COALESCE(taken_at, slot) < ?
            ORDER BY COALESCE(taken_at, slot)""",
        (start.isoformat(), (end + timedelta(days=1)).isoformat()),
    ).fetchall()
    for log in logs:
        row = rows.get((log["medication_id"], log["kind"])) or next(
            (r for (mid, _), r in rows.items() if mid == log["medication_id"]), None
        )
        if row is None:
            continue
        dn = day_number(cycle, date.fromisoformat((log["taken_at"] or log["slot"])[:10]))
        if dn in row.cells:
            (row.cells[dn].taken if log["status"] == "taken" else row.cells[dn].skipped).append(log)
    sym_rows: dict[str, SymptomRow] = {}
    for entry in symptoms_between(conn, start, end):
        key = entry["symptom"] if entry["symptom"] != "other" else f"other:{entry['label'] or ''}"
        if key not in sym_rows:
            name = sx.lookup(entry["symptom"]).name if entry["symptom"] != "other" else (entry["label"] or "Other")
            sym_rows[key] = SymptomRow(entry["symptom"], name, {d: [] for d in days})
        dn = day_number(cycle, date.fromisoformat(entry["logged_at"][:10]))
        if dn in sym_rows[key].cells:
            sym_rows[key].cells[dn].append(entry)
    order = {"clinic": 0, "scheduled": 1, "as_needed": 2}
    med_rows = sorted(rows.values(), key=lambda r: (order[r.kind], r.name))
    symptom_order = {s.key: i for i, s in enumerate(sx.SYMPTOMS)}
    return days, med_rows, sorted(sym_rows.values(), key=lambda r: (symptom_order.get(r.symptom, 99), r.name))
