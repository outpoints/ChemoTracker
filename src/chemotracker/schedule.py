"""Cycle-day arithmetic, dose slots, and the views built on them.

Planned doses are not stored. They are computed from each cycle's Day 1 and
the medicine plans, then matched to dose_logs by (plan_id, slot) once for all
cycles together (see _pair_logs). Editing a plan therefore never leaves stale
"planned" rows behind, and every screen sees the same pairing.

Day numbering follows the clinic sheet: Day 1 is chemo day, Day 0 is the day
before chemo.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import symptoms as sx

DUE_BEFORE = timedelta(minutes=30)
DUE_AFTER = timedelta(minutes=60)
FOLLOW_UP_AFTER = timedelta(minutes=30)
FOLLOW_UP_WITHIN = timedelta(hours=24)
# A dose logged without a planned time fills a dose that wasn't due yet only when given at most this much ahead.
EARLY_PAIR = timedelta(hours=2)
# Clock drift allowed when logging: a time up to this far ahead of now is accepted, and counts at once.
AHEAD_OK = timedelta(minutes=5)
# "Up to N a day" on a label means in 24 hours (DailyMed, FDA and MedlinePlus word limits that way).
DAY_WINDOW = timedelta(hours=24)

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


def latest_reading(local: datetime) -> datetime:
    """A stored time the clocks showed twice (the hour repeated in autumn) or skipped, read the cautious way: the
    later of its two possible moments. Waits counted from it then never end early."""
    if _TZ is None:
        return local
    return max(local.replace(fold=0), local.replace(fold=1), key=_utc)


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


def fmt_hours(hours: float) -> str:
    """'6 hr', '4 hr 15 min', '30 min': a label's gap shown exactly, never rounded."""
    return fmt_duration(timedelta(minutes=round(hours * 60)))


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
    """The latest cycle whose Day 0 is on or before d. Today's header ("Day 3 of 14") counts from it.

    Neighbouring cycles can both plan doses on one date (the next cycle's Day 0 is often the last day of the one
    before), so the doses on a date come from slots_on(), never from this cycle alone.
    """
    found = None
    for c in all_cycles(conn):
        if d >= day1_of(c) - timedelta(days=1):
            found = c
    return found


def next_cycle_after(conn: sqlite3.Connection, d: date) -> sqlite3.Row | None:
    return next((c for c in all_cycles(conn) if day1_of(c) - timedelta(days=1) > d), None)


def _next_day1(cycles: list[sqlite3.Row], cycle: sqlite3.Row) -> date | None:
    """Day 1 of the cycle after this one, if there is one."""
    later = [day1_of(c) for c in cycles if day1_of(c) > day1_of(cycle)]
    return min(later) if later else None


# ---------------------------------------------------------------- plans

PLAN_COLUMNS = """p.*, m.name AS med_name, m.brand AS med_brand, m.info_key AS med_info, m.route AS med_route,
       m.strength AS med_strength, m.dose AS med_dose, m.directions AS med_directions,
       m.min_hours_between AS med_gap, m.max_per_day AS med_max,
       m.prep_minutes AS med_prep, m.prep_note AS med_prep_note, m.active AS med_active"""

PLAN_SQL = f"""
SELECT {PLAN_COLUMNS}
  FROM plans p JOIN medications m ON m.id = p.medication_id
 WHERE m.active = 1 AND p.from_cycle <= :n AND (p.to_cycle IS NULL OR p.to_cycle >= :n)
 ORDER BY CASE p.kind WHEN 'clinic' THEN 0 WHEN 'scheduled' THEN 1 ELSE 2 END, m.name, p.id
"""

# Every plan, stopped medicines included: the record keeps their doses from before they were stopped.
ALL_PLANS_SQL = f"""
SELECT {PLAN_COLUMNS}
  FROM plans p JOIN medications m ON m.id = p.medication_id
 ORDER BY CASE p.kind WHEN 'clinic' THEN 0 WHEN 'scheduled' THEN 1 ELSE 2 END, m.name, p.id
"""


def plans_for(conn: sqlite3.Connection, cycle: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute(PLAN_SQL, {"n": cycle["number"]}).fetchall()


def _in_cycle(plan: sqlite3.Row, cycle: sqlite3.Row) -> bool:
    n = cycle["number"]
    return plan["from_cycle"] <= n and (plan["to_cycle"] is None or plan["to_cycle"] >= n)


def plan_end(plan: sqlite3.Row, cycle: sqlite3.Row, next_day1: date | None = None) -> int:
    """The plan's last day in this cycle.

    An open end ("Every day of the cycle", "Day 3 to end of cycle") runs to the cycle's length. When the next
    cycle starts later than that (chemo delayed), it keeps going until the day before the next Day 1, so daily
    medicines don't stop on the delay days.
    """
    if plan["end_day"] is not None:
        return plan["end_day"]
    end = int(cycle["length_days"])
    if next_day1 is not None:
        end = max(end, (next_day1 - day1_of(cycle)).days)
    return end


def plan_times(plan: sqlite3.Row) -> list[str]:
    return [t for t in (plan["times"] or "").split(",") if t]


def cycle_span(conn: sqlite3.Connection, cycle: sqlite3.Row) -> int:
    nd1 = _next_day1(all_cycles(conn), cycle)
    last = max((plan_end(p, cycle, nd1) for p in plans_for(conn, cycle)), default=0)
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
        if real_diff(now, at) >= timedelta(0):
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
SELECT dl.*, m.name AS med_name, m.active AS med_active, who.name AS who
  FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
  LEFT JOIN profiles who ON who.id = COALESCE(dl.updated_by, dl.logged_by)
 ORDER BY COALESCE(dl.taken_at, dl.slot), dl.id
"""


def _log_time(log: sqlite3.Row) -> datetime | None:
    return parse_local(log["taken_at"]) or (parse_local(log["slot"]) if log["slot"] and len(log["slot"]) > 10 else None)


def home_date(log: sqlite3.Row) -> date | None:
    """The day a dose that fits no slot is shown on: the planned day it was logged for, else the day it was taken."""
    if log["slot"]:
        return date.fromisoformat(log["slot"][:10])
    taken = parse_local(log["taken_at"])
    return taken.date() if taken else None


def is_took_one(log: sqlite3.Row) -> bool:
    """A dose from the as-needed shelf ("Took one"): logged against an as-needed plan, with no planned time.

    It stays an as-needed dose even if that plan is later changed to a schedule, so it never fills a planned dose.
    """
    return not log["slot"] and log["plan_id"] is not None and log["kind"] == "as_needed"


def _pair_logs(slots: list[Slot], logs: list[sqlite3.Row], prn_covers, took_one: frozenset[int] = frozenset()) -> dict[int, Slot]:
    """Attach each logged dose to its planned slot. Returns {log id: slot}. One log fills at most one slot.

    A dose is tied to its slot by plan and planned time. A dose logged for a planned dose keeps counting when the
    schedule changes afterwards (a new time, new days, a replaced plan): it goes to the nearest open slot of the same
    medicine on the day it was logged for, its own plan first. Otherwise the row would show as due again and the
    family could give a second dose.

    A dose logged without a planned time ("Log a dose") goes to the nearest open dose of that medicine that was
    already due when it was taken: a late morning dose fills the morning, never the evening. A dose taken in the
    small hours can be last night's evening dose, and one taken just before midnight can be a dose planned for just
    after it. It fills a dose that wasn't due yet only when it was given at most EARLY_PAIR ahead and no due dose of
    that medicine was open. It never fills a clinic dose, and on a day the medicine is also allowed as needed it is
    left alone, because it may be the as-needed one. "Took one" doses never fill a planned dose.
    """
    paired: dict[int, Slot] = {}
    by_key = {(s.plan["id"], s.key): s for s in slots}
    for log in logs:
        if log["status"] not in ("taken", "skipped") or log["plan_id"] is None or not log["slot"]:
            continue
        s = by_key.get((log["plan_id"], log["slot"]))
        if s is not None and s.log is None:
            s.log = log
            paired[log["id"]] = s
    open_slots: dict[tuple[int, date], list[Slot]] = {}
    for s in slots:
        if s.log is None:
            open_slots.setdefault((s.plan["medication_id"], s.date), []).append(s)
    pairs: list[tuple[tuple, sqlite3.Row, Slot]] = []
    for log in logs:
        if log["id"] in paired or log["status"] not in ("taken", "skipped"):
            continue
        mid, at = log["medication_id"], _log_time(log)
        if log["slot"]:  # logged for a planned dose whose time, days or plan changed since
            d = date.fromisoformat(log["slot"][:10])
            for s in open_slots.get((mid, d), []):
                if s.kind == "clinic" and not (log["kind"] == "clinic" or log["plan_id"] == s.plan["id"]):
                    continue
                gap = abs(real_diff(at, s.dt).total_seconds()) if (at is not None and s.time) else 0.0
                pairs.append(((0, 0 if log["plan_id"] == s.plan["id"] else 1, gap, log["id"]), log, s))
            continue
        if is_took_one(log) or log["id"] in took_one or at is None:
            continue
        if prn_covers(mid, at.date()):
            continue  # may be the as-needed dose: don't guess
        for d in (at.date() - timedelta(days=1), at.date(), at.date() + timedelta(days=1)):
            for s in open_slots.get((mid, d), []):
                if s.kind == "clinic" or not s.time:
                    continue  # a dose given at home never fills a clinic dose
                if d < at.date() and (at.hour >= 5 or s.time < "18:00"):
                    continue  # the small hours only belong to last night's evening dose
                ahead = real_diff(s.dt, at)
                if ahead <= DUE_BEFORE:
                    tier = 0  # it was due (or past) when it was taken
                elif ahead <= EARLY_PAIR:
                    tier = 1  # given a little early
                else:
                    continue  # a dose that wasn't due yet: never filled by an earlier dose
                pairs.append(((1, tier, abs(ahead.total_seconds()), log["id"]), log, s))
    for _, log, s in sorted(pairs, key=lambda p: p[0]):
        if log["id"] not in paired and s.log is None:
            s.log = log
            paired[log["id"]] = s
    return paired


@dataclass
class _Index:
    """Every planned dose of every cycle, paired with the logged doses ONCE, so all screens agree."""
    cycles: list[sqlite3.Row]
    slots: list[Slot]
    by_key: dict[tuple[int, str], Slot]
    by_date: dict[date, list[Slot]]
    logs: list[sqlite3.Row]
    paired: dict[int, Slot]
    prn: dict[int, list[tuple[date, date | None, sqlite3.Row]]]  # medicine -> as-needed date ranges
    took_one: frozenset[int] = frozenset()  # "Took one" doses whose as-needed plan was deleted since

    def prn_covers(self, medication_id: int, d: date) -> bool:
        return self.prn_plan(medication_id, d) is not None

    def prn_plan(self, medication_id: int, d: date) -> sqlite3.Row | None:
        for start, end, plan in self.prn.get(medication_id, ()):
            if start <= d and (end is None or d <= end):
                return plan
        return None


def _stopped_at(conn: sqlite3.Connection, plans: list[sqlite3.Row]) -> dict[int, datetime]:
    """When each stopped medicine was stopped (local time), from the change history, else its last edit."""
    stopped = {p["medication_id"] for p in plans if not p["med_active"]}
    out: dict[int, datetime] = {}
    if not stopped:
        return out
    marks = ",".join("?" * len(stopped))
    for row in conn.execute(f"""SELECT ref, at, before, after FROM history WHERE entity = 'medication'
                                   AND action = 'changed' AND ref IN ({marks}) ORDER BY id""",
                            [str(m) for m in stopped]):
        try:
            before, after = json.loads(row["before"] or "{}"), json.loads(row["after"] or "{}")
        except ValueError:
            continue
        if after.get("active") == 0 and before.get("active", 1) == 1:
            out[int(row["ref"])] = local_from_utc(row["at"])
    for row in conn.execute(f"SELECT id, updated_at FROM medications WHERE id IN ({marks})", list(stopped)):
        if row["id"] not in out:
            out[row["id"]] = local_from_utc(row["updated_at"])
    return out


def _took_one_ids(conn: sqlite3.Connection, logs: list[sqlite3.Row]) -> frozenset[int]:
    """Doses that came from "Took one" but lost their plan (deleting a plan clears the dose's plan_id). The change
    history still has how each was created, so they stay as-needed doses and never fill a planned dose."""
    free = [str(log["id"]) for log in logs if not log["slot"] and log["plan_id"] is None and log["kind"] == "as_needed"]
    out: set[int] = set()
    for i in range(0, len(free), 500):
        chunk = free[i:i + 500]
        latest: dict[str, str | None] = {}
        for row in conn.execute(f"""SELECT ref, after FROM history WHERE entity = 'dose' AND action = 'created'
                                       AND ref IN ({",".join("?" * len(chunk))}) ORDER BY id""", chunk):
            latest[row["ref"]] = row["after"]
        for ref, after in latest.items():
            try:
                snap = json.loads(after or "{}")
            except ValueError:
                continue
            if snap.get("plan_id") is not None and not snap.get("slot") and snap.get("kind") == "as_needed":
                out.add(int(ref))
    return frozenset(out)


def _build_index(conn: sqlite3.Connection) -> _Index:
    cycles = all_cycles(conn)
    plans = conn.execute(ALL_PLANS_SQL).fetchall()
    stopped = _stopped_at(conn, plans)
    found: dict[tuple[int, str], Slot] = {}
    prn: dict[int, list[tuple[date, date | None, sqlite3.Row]]] = {}
    for c in cycles:
        nd1 = _next_day1(cycles, c)
        for p in plans:
            if not _in_cycle(p, c):
                continue
            if p["kind"] == "as_needed":
                if p["med_active"]:
                    start = date_of(c, p["start_day"])
                    if p["end_day"] is not None:
                        end: date | None = date_of(c, p["end_day"])
                    else:  # every day until the next cycle starts, or with no next cycle, from here on
                        end = max(date_of(c, int(c["length_days"])), nd1 - timedelta(days=1)) if nd1 else None
                    prn.setdefault(p["medication_id"], []).append((start, end, p))
                continue
            cut = stopped.get(p["medication_id"]) if not p["med_active"] else None
            for dn in range(p["start_day"], plan_end(p, c, nd1) + 1):
                d = date_of(c, dn)
                for t in plan_times(p) if p["kind"] == "scheduled" else [None]:
                    s = Slot(plan=p, cycle=c, day=dn, date=d, time=t)
                    if cut is not None and s.dt >= cut:
                        continue  # planned after the medicine was stopped
                    found[(p["id"], s.key)] = s  # the same dose in two cycles is one dose: the later cycle's
    slots = sorted(found.values(), key=lambda s: (s.date, s.kind != "clinic", s.time or "", s.plan["med_name"],
                                                  s.plan["id"]))
    logs = conn.execute(LOG_SQL).fetchall()
    idx = _Index(cycles=cycles, slots=slots, by_key=found, by_date={}, logs=logs, paired={}, prn=prn,
                 took_one=_took_one_ids(conn, logs))
    for s in slots:
        idx.by_date.setdefault(s.date, []).append(s)
    idx.paired = _pair_logs(slots, logs, idx.prn_covers, idx.took_one)
    return idx


# One pairing per connection and database state: a page asks for it many times, and it must not change between
# those questions unless something was written.
_MEMO: dict[int, tuple] = {}
_MEMO_LOCK = threading.Lock()


def _index(conn: sqlite3.Connection) -> _Index:
    stamp = (conn.total_changes, id(_TZ))
    with _MEMO_LOCK:
        hit = _MEMO.get(id(conn))
        if hit is not None and hit[0] is conn and hit[1] == stamp:
            return hit[2]
    idx = _build_index(conn)
    with _MEMO_LOCK:
        _MEMO[id(conn)] = (conn, stamp, idx)
        while len(_MEMO) > 8:
            _MEMO.pop(next(iter(_MEMO)))
    return idx


def slots(conn: sqlite3.Connection, cycle: sqlite3.Row, day: int | None = None) -> list[Slot]:
    """Every planned clinic/scheduled dose in a cycle (or one day of it), each with the dose logged for it.

    Logged doses are paired across all cycles at once, so a day always shows the same thing whichever way it's
    looked at, and one logged dose never counts in two cycles. A dose planned by two overlapping cycles on the
    same date (a plan that runs from Day 0 to the end of the cycle) belongs to the later one.
    """
    return [s for s in _index(conn).slots if s.cycle["id"] == cycle["id"] and (day is None or s.day == day)]


def slots_on(conn: sqlite3.Connection, d: date, include_stopped: bool = False) -> list[Slot]:
    """Every planned dose on date `d`, from every cycle that plans one then. Today, Taken, the logging checks,
    \"From last night\" and the reminders all read the day from here."""
    return [s for s in _index(conn).by_date.get(d, []) if include_stopped or s.plan["med_active"]]


def extra_doses(conn: sqlite3.Connection, cycle: sqlite3.Row | None, d: date) -> list[dict]:
    """Doses logged on `d` that fit no planned slot, so Today can show them instead of hiding them.

    `note` says why it matters: more doses than planned, or a planned medicine on a day it isn't planned. As-needed
    doses on a day the as-needed shelf shows that medicine are left to the shelf. A stopped medicine's doses are
    listed here too, because Today no longer has rows for it. `cycle` is accepted for older callers and ignored.
    """
    idx = _index(conn)
    planned_today = {s.plan["medication_id"] for s in idx.by_date.get(d, []) if s.plan["med_active"]}
    planned_ever = {s.plan["medication_id"] for s in idx.slots if s.plan["med_active"]}
    rows = []
    for log in idx.logs:
        if log["status"] not in ("taken", "skipped"):
            continue
        s = idx.paired.get(log["id"])
        if s is not None:
            if s.plan["med_active"] or s.date != d:
                continue
            note = ""
        else:
            if home_date(log) != d:
                continue
            mid = log["medication_id"]
            if not log["slot"] and log["med_active"] and idx.prn_covers(mid, d):
                continue  # the as-needed shelf shows it
            if is_took_one(log) or log["id"] in idx.took_one:
                note = ""
            else:
                note = ("More than planned. Check it isn't the same dose logged twice." if mid in planned_today
                        else "Not on the schedule this day." if mid in planned_ever else "")
        rows.append({**dict(log), "note": note})
    return rows


def find_slot(conn: sqlite3.Connection, plan_id: int, key: str) -> Slot | None:
    return _index(conn).by_key.get((plan_id, key))


def find_moved_slot(conn: sqlite3.Connection, plan_id: int | None, medication_id: int | None, key: str) -> Slot | None:
    """The dose a row from an older page means after its schedule changed: that plan's (else that medicine's)
    dose on the same day, nearest the time the page showed. None when the medicine isn't planned that day."""
    try:
        d = date.fromisoformat(key[:10])
    except ValueError:
        return None
    if medication_id is None and plan_id is not None:
        row = next((s for s in _index(conn).slots if s.plan["id"] == plan_id), None)
        medication_id = row.plan["medication_id"] if row else None
    if medication_id is None:
        return None
    found = [s for s in slots_on(conn, d) if s.plan["medication_id"] == medication_id]
    if not found:
        return None
    old = parse_local(key) if len(key) > 10 else None

    def rank(s: Slot) -> tuple:
        distance = abs(real_diff(s.dt, old).total_seconds()) if (old is not None and s.time) else 0.0
        return (0 if s.plan["id"] == plan_id else 1, distance)
    return min(found, key=rank)


def paired_slot(conn: sqlite3.Connection, log_id: int) -> Slot | None:
    return _index(conn).paired.get(log_id)


def slot_for_new_dose(conn: sqlite3.Connection, medication_id: int, at: datetime) -> Slot | None:
    """The planned dose a dose logged now from "Log a dose" fills, by the same rule the pairing uses."""
    idx = _index(conn)
    fake = {"id": -1, "medication_id": medication_id, "plan_id": None, "kind": "as_needed", "slot": None,
            "status": "taken", "taken_at": fmt_local(at)}
    probe = [Slot(plan=s.plan, cycle=s.cycle, day=s.day, date=s.date, time=s.time, log=s.log)
             for d in (at.date() - timedelta(days=1), at.date(), at.date() + timedelta(days=1))
             for s in idx.by_date.get(d, []) if s.plan["medication_id"] == medication_id and s.plan["med_active"]]
    paired = _pair_logs(probe, [fake], idx.prn_covers)  # type: ignore[list-item]
    return paired.get(-1)


def prn_covers(conn: sqlite3.Connection, medication_id: int, d: date) -> bool:
    """Whether an as-needed plan allows this medicine on date d."""
    return _index(conn).prn_covers(medication_id, d)


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
    count_today: int  # taken in the last 24 hours ("up to N a day" on a label means in 24 hours)
    gap_hours: float | None
    max_per_day: int | None
    scheduled_today: bool = True
    schedule_note: str = ""
    next_day_ok: bool = True  # False when the next OK time falls on a day the plan doesn't allow it
    all_today: bool = True  # every dose counted toward the limit was taken today
    day_doses: list = field(default_factory=list)  # doses taken on the day being looked at (past days)

    @property
    def last_at(self) -> datetime | None:
        return parse_local(self.last["taken_at"]) if self.last else None

    @property
    def next_allowed(self) -> datetime | None:
        if self.last_at and self.gap_hours:
            # A time the clocks show twice is read as the later one, so the next dose is never OK early.
            return real_add(latest_reading(self.last_at), timedelta(hours=self.gap_hours))
        return None

    def remaining(self, now: datetime) -> timedelta:
        """Real time until the next one is OK (never wall-clock: on the night the clocks change they differ)."""
        nxt = self.next_allowed
        return max(real_diff(nxt, now), timedelta(0)) if nxt is not None else timedelta(0)

    def at_max(self) -> bool:
        return self.max_per_day is not None and self.count_today >= self.max_per_day

    @property
    def gap_known(self) -> bool:
        return bool(self.gap_hours)

    def available(self, now: datetime) -> bool:
        if self.at_max() or not self.scheduled_today:
            return False
        nxt = self.next_allowed
        return nxt is None or real_diff(nxt, now) <= timedelta(0)

    def state(self, now: datetime) -> str:
        """off (not planned today), max (daily limit reached), wait, ended, ok, or open.

        open means no wait time is saved, so the app can't say when the next one is OK.
        It must never look like "OK to take now". ended means the next OK time falls on a day the plan
        doesn't allow it, so there is nothing to count down to.
        """
        if not self.scheduled_today:
            return "off"
        if self.at_max():
            return "max"
        if not self.available(now):
            return "wait" if self.next_day_ok else "ended"
        return "ok" if self.gap_known else "open"

    def fraction(self, now: datetime) -> float:
        """How far through the waiting gap we are (1.0 = OK now, 0.0 when the gap isn't known)."""
        gap = self.gap_hours
        if self.at_max() or not self.scheduled_today or not gap or not self.next_day_ok:
            return 0.0
        if not self.last_at or self.available(now):
            return 1.0
        total = gap * 3600
        return min(max(1.0 - self.remaining(now).total_seconds() / total, 0.0), 1.0)

    def status_text(self, now: datetime) -> str:
        if not self.scheduled_today:
            return f"Not planned today ({self.schedule_note})" if self.schedule_note else "Not planned today"
        if self.at_max():
            return f"Reached {self.max_per_day} today" if self.all_today else f"Reached {self.max_per_day} in 24 hours"
        if self.available(now):
            return "OK to take now" if self.gap_known else "Time between doses not saved"
        if not self.next_day_ok:
            return f"Not planned after today ({self.schedule_note})" if self.schedule_note else "Not planned after today"
        nxt = self.next_allowed
        assert nxt is not None
        prefix = "OK at" if nxt.date() == now.date() else f"OK {fmt_relative_day(nxt.date(), now.date()).lower()} at"
        return f"{prefix} {fmt_time(nxt)}"

    def wait_text(self, now: datetime) -> str:
        st = self.state(now)
        if st == "open":
            return "Follow the label"
        if st != "wait":
            return ""
        return f"in {fmt_duration(self.remaining(now))}"


# Two people giving the same medicine minutes apart is the classic double dose.
RECENT_DUPLICATE = timedelta(minutes=30)


def _readings(dt: datetime) -> tuple[datetime, datetime]:
    return dt.replace(fold=0), dt.replace(fold=1)


def _apart(a: datetime, b: datetime) -> timedelta:
    """Real time between two wall-clock times, the cautious way: a time the clocks show twice counts as
    whichever reading puts the two doses closer together."""
    return min(abs(real_diff(x, y)) for x in _readings(a) for y in _readings(b))


def early_dose_warning(conn: sqlite3.Connection, med: sqlite3.Row, when: datetime, by: int | None = None,
                       exclude_id: int | None = None) -> str | None:
    """A plain sentence if logging a dose at `when` looks like a double dose, or breaks the label's gap or limit.

    The label's "up to N a day" counts the 24 hours around the dose, not the calendar day.
    """
    gap, most = med["min_hours_between"], med["max_per_day"]
    limit = timedelta(hours=gap) if gap else RECENT_DUPLICATE
    window = limit + timedelta(hours=2)  # wall-clock search; the real gap is checked below
    near = conn.execute(
        """SELECT dl.taken_at, dl.logged_by, p.name AS who FROM dose_logs dl LEFT JOIN profiles p ON p.id = dl.logged_by
            WHERE dl.medication_id = ? AND dl.status = 'taken' AND dl.taken_at > ? AND dl.taken_at < ? AND dl.id != ?
            ORDER BY abs(julianday(dl.taken_at) - julianday(?)) LIMIT 1""",
        (med["id"], fmt_local(when - window), fmt_local(when + window), exclude_id or -1, fmt_local(when)),
    ).fetchone()
    if near:
        other = parse_local(near["taken_at"])
        assert other is not None
        apart = _apart(when, other)
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
        rows = conn.execute(
            """SELECT taken_at FROM dose_logs WHERE medication_id = ? AND status = 'taken' AND taken_at > ?
                AND taken_at < ? AND id != ?""",
            (med["id"], fmt_local(when - timedelta(hours=26)), fmt_local(when + timedelta(hours=26)), exclude_id or -1),
        ).fetchall()
        count = sum(1 for r in rows if _apart(when, parse_local(r["taken_at"])) < DAY_WINDOW)  # type: ignore[arg-type]
        if count >= most:
            return (f"{count} {'dose is' if count == 1 else 'doses are'} already logged within 24 hours of this one. "
                    f"The label allows up to {most} in 24 hours.")
    return None


# White-cell boosters must wait 24 hours after chemo (Neupogen label: "at least 24 hours after chemotherapy";
# Neulasta label: not "between 14 days before and 24 hours after administration of chemotherapy"). Only
# cytotoxic chemo starts that clock, not premeds or antibody-only infusions.
BOOSTERS = ("filgrastim", "pegfilgrastim")
ANTIBODY_ONLY = ("trastuzumab", "pertuzumab", "pembrolizumab")
BOOSTER_WAIT = timedelta(hours=24)


def booster_warning(conn: sqlite3.Connection, med: sqlite3.Row, when: datetime, exclude_id: int | None = None) -> str | None:
    """A plain sentence when a white-cell booster is logged less than 24 hours after the latest logged chemo."""
    if med["info_key"] not in BOOSTERS:
        return None
    marks = ",".join("?" * len(ANTIBODY_ONLY))
    row = conn.execute(
        f"""SELECT dl.taken_at, m.name FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
             WHERE m.category = 'infusion' AND COALESCE(m.info_key, '') NOT IN ({marks}) AND dl.status = 'taken'
               AND dl.taken_at IS NOT NULL AND dl.taken_at <= ? AND dl.taken_at > ? AND dl.id != ?
             ORDER BY dl.taken_at DESC LIMIT 1""",
        (*ANTIBODY_ONLY, fmt_local(when), fmt_local(when - timedelta(hours=26)), exclude_id or -1),
    ).fetchone()
    if row is None:
        return None
    chemo = parse_local(row["taken_at"])
    assert chemo is not None
    apart = _apart(when, chemo)
    if apart >= BOOSTER_WAIT:
        return None
    day = "" if chemo.date() == when.date() else f"{fmt_relative_day(chemo.date(), when.date()).lower()} "
    return (f"{med['name']} is meant to be given at least 24 hours after chemo. {row['name']} was logged {day}at "
            f"{fmt_time(chemo)}, {fmt_duration(apart)} before this dose. Check with your care team before giving it.")


def _who_logged(log: sqlite3.Row, by: int | None) -> str:
    if by is not None and (log["updated_by"] or log["logged_by"]) == by:
        return "you"
    return log["who"] if "who" in log.keys() and log["who"] else "someone"


def _plan_days(plans: list[sqlite3.Row]) -> str:
    return ", ".join(dict.fromkeys(days_text(p["start_day"], p["end_day"]) for p in plans))


def dose_warning(conn: sqlite3.Connection, med: sqlite3.Row, when: datetime, by: int | None = None,
                 slot: Slot | None = None, now: datetime | None = None, unplanned_ok: bool = False,
                 exclude_id: int | None = None) -> str | None:
    """A plain sentence when logging this dose could be a double dose (or otherwise needs a second look), or None.

    Every way of logging a dose asks this first: a scheduled row's Taken or Given, Took all, Log a dose, Took one,
    and moving a dose on its page (`exclude_id` is that dose). As-needed medicines keep the label's gap and limit
    (early_dose_warning). Planned medicines are also compared with that day's schedule: all of the day's doses
    already logged, another dose less than half the planned gap away, or a medicine on a day it isn't planned.
    A white-cell booster less than 24 hours after chemo is flagged as well.
    """
    first = early_dose_warning(conn, med, when, by, exclude_id) or \
        _planned_warning(conn, med, when, by, slot, now, unplanned_ok, exclude_id)
    booster = booster_warning(conn, med, when, exclude_id)
    return " ".join(x for x in (first, booster) if x) or None


def _planned_warning(conn: sqlite3.Connection, med: sqlite3.Row, when: datetime, by: int | None, slot: Slot | None,
                     now: datetime | None, unplanned_ok: bool, exclude_id: int | None) -> str | None:
    idx = _index(conn)
    mid = med["id"]
    d = slot.date if slot is not None else when.date()
    covered = idx.prn_covers(mid, d)
    if covered and (slot is None or med["min_hours_between"]):
        return None  # allowed as needed that day: the label's gap and daily limit are the rule
    mine = [s for s in idx.by_date.get(d, []) if s.plan["medication_id"] == mid and s.plan["med_active"]]
    today = (now or when).date()
    if d == today:
        is_word, isnt_word, day_word = "is", "isn't", "today"
    else:
        is_word, isnt_word = "was", "wasn't"
        day_word = "yesterday" if (today - d).days == 1 else f"on {fmt_day(d)}"
    if not mine:
        if unplanned_ok:
            return None
        plans = conn.execute("SELECT * FROM plans WHERE medication_id = ? ORDER BY start_day", (mid,)).fetchall()
        planned = [p for p in plans if p["kind"] != "as_needed"]
        if planned:
            return (f"{med['name']} {isnt_word} planned {day_word}. Your schedule has it on {_plan_days(planned)}. "
                    "Make sure it wasn't already given and that the care team wants this dose.")
        prn_plans = [p for p in plans if p["kind"] == "as_needed"]
        if prn_plans and not covered:
            return (f"{med['name']} {isnt_word} planned {day_word}. Your schedule allows it as needed on "
                    f"{_plan_days(prn_plans)}. Check with your care team before taking it on other days.")
        return None
    this = (slot.plan["id"], slot.key) if slot is not None else None
    taken = [s.log for s in mine if s.log is not None and s.log["status"] == "taken" and s.log["id"] != exclude_id
             and (s.plan["id"], s.key) != this]
    taken += [log for log in idx.logs if log["medication_id"] == mid and log["status"] == "taken"
              and log["id"] not in idx.paired and log["id"] != exclude_id and home_date(log) == d]
    # Two schedules of one medicine at the same minute are one dose (a schedule saved twice, for example).
    planned_count = len({s.time for s in mine if s.time}) + sum(1 for s in mine if not s.time)
    if taken and len(taken) >= planned_count:
        last = max(taken, key=lambda log: log["taken_at"] or "")
        who = _who_logged(last, by)
        at = f" at {fmt_time(last['taken_at'])}" if last["taken_at"] else ""
        if planned_count == 1:
            return (f"{med['name']} {is_word} planned once {day_word}, and {who} already logged it{at}. "
                    "Make sure it isn't the same dose logged twice.")
        return (f"{med['name']} {is_word} planned {planned_count} times {day_word}, and all {planned_count} are "
                f"already logged, the last{at}. Make sure it isn't the same dose logged twice.")
    times = sorted({s.dt for s in mine if s.time})
    if times:
        steps = [b - a for a, b in zip(times, times[1:]) if b > a]
        gap = min(steps) if steps else timedelta(hours=24)
        near = conn.execute(
            """SELECT dl.*, p.name AS who FROM dose_logs dl LEFT JOIN profiles p ON p.id = COALESCE(dl.updated_by, dl.logged_by)
                WHERE dl.medication_id = ? AND dl.status = 'taken' AND dl.taken_at > ? AND dl.taken_at < ? AND dl.id != ?
                ORDER BY abs(julianday(dl.taken_at) - julianday(?)) LIMIT 1""",
            (mid, fmt_local(when - gap), fmt_local(when + gap), exclude_id or -1, fmt_local(when)),
        ).fetchone()
        if near is not None:
            other = parse_local(near["taken_at"])
            assert other is not None
            apart = _apart(when, other)
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
             AND taken_at <= ? ORDER BY taken_at DESC, id DESC LIMIT 1""",
        (medication_id, fmt_local(before)),
    ).fetchone()


def _as_needed_row(conn: sqlite3.Connection, med: sqlite3.Row, plan: sqlite3.Row | None, now: datetime) -> AsNeeded:
    """One as-needed medicine as it stands at `now`: the last dose, the doses in the last 24 hours, and whether
    an as-needed plan allows it today (and on the day the next one would be OK)."""
    upto = now + AHEAD_OK  # a dose logged a few minutes ahead (clock drift) counts at once
    rows = conn.execute(
        """SELECT taken_at FROM dose_logs WHERE medication_id = ? AND status = 'taken' AND taken_at > ? AND taken_at <= ?""",
        (med["id"], fmt_local(now - timedelta(hours=26)), fmt_local(upto)),
    ).fetchall()
    recent = [t for t in (parse_local(r["taken_at"]) for r in rows)
              if t is not None and real_diff(now, latest_reading(t)) < DAY_WINDOW]
    an = AsNeeded(
        medication_id=med["id"],
        name=med["name"],
        brand=med["brand"],
        strength=med["strength"],
        dose=med["dose"],
        info_key=med["info_key"],
        plan=plan,
        last=last_taken(conn, med["id"], upto),
        count_today=len(recent),
        gap_hours=med["min_hours_between"],
        max_per_day=med["max_per_day"],
        all_today=all(t.date() == now.date() for t in recent),
    )
    prn_plans = conn.execute("SELECT * FROM plans WHERE medication_id = ? AND kind = 'as_needed' ORDER BY start_day",
                             (med["id"],)).fetchall()
    if prn_plans:
        idx = _index(conn)
        covering = idx.prn_plan(med["id"], now.date())
        an.schedule_note = " and ".join(dict.fromkeys(days_text(p["start_day"], p["end_day"]) for p in prn_plans))
        if covering is None or not med["active"]:
            an.scheduled_today = False  # respect the clinic's day limits (e.g. "Days 4-6 only")
        elif an.plan is None:
            an.plan = covering
        nxt = an.next_allowed
        if nxt is not None and nxt.date() > now.date() and not idx.prn_covers(med["id"], nxt.date()):
            an.next_day_ok = False
    return an


def as_needed_for_day(conn: sqlite3.Connection, cycle: sqlite3.Row | None, d: date, now: datetime) -> list[AsNeeded]:
    """The as-needed shelf for date d: every active medicine an as-needed plan allows that day, from any cycle.

    The state is always now's (when the next one is OK). For an earlier day, `day_doses` lists that day's doses so
    the page can show a record instead of offering a dose for today. `cycle` is accepted for older callers.
    """
    idx = _index(conn)
    out = []
    for mid in idx.prn:
        plan = idx.prn_plan(mid, d)
        if plan is None:
            continue
        med = conn.execute("SELECT * FROM medications WHERE id = ? AND active = 1", (mid,)).fetchone()
        if med is None:
            continue
        an = _as_needed_row(conn, med, plan, now)
        if d < now.date():
            an.day_doses = [log for log in idx.logs if log["medication_id"] == mid and log["status"] == "taken"
                            and log["taken_at"] and log["taken_at"][:10] == d.isoformat()
                            and log["id"] not in idx.paired and not log["slot"]]
        out.append(an)
    out.sort(key=lambda a: a.name)
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
    out = [_as_needed_row(conn, m, None, now) for m in meds]
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
        order = {"ok": 0, "open": 1, "wait": 2, "max": 3, "ended": 4, "off": 5}
        return sorted(self.as_needed, key=lambda a: (order[a.state(now)], a.next_allowed or now, a.name))


def day_plan(conn: sqlite3.Connection, d: date, now: datetime) -> DayPlan:
    cycle = cycle_for_date(conn, d)
    day_slots = slots_on(conn, d)
    if cycle is None and not day_slots:
        return DayPlan(None, None, d, [], [], [])
    groups: dict[str, list[Slot]] = {key: [] for key, _, _ in PERIODS}
    for s in day_slots:
        if s.kind == "scheduled":
            groups[s.period].append(s)
    return DayPlan(
        cycle=cycle,
        day=day_number(cycle, d) if cycle is not None else None,
        date=d,
        clinic=[s for s in day_slots if s.kind == "clinic"],
        compartments=[Compartment(k, PERIOD_NAMES[k], v) for k, v in groups.items() if v],
        as_needed=as_needed_for_day(conn, cycle, d, now),
    )


def next_doses(conn: sqlite3.Connection, now: datetime, limit: int = 3) -> list[Slot]:
    """Unlogged scheduled doses from 'due now' onward, over the next few days, from every cycle."""
    found = [s for k in range(-1, 15) for s in slots_on(conn, now.date() + timedelta(days=k))
             if s.kind == "scheduled" and s.log is None and s.dt >= now - DUE_AFTER]
    found.sort(key=lambda s: s.dt)
    return found[:limit]


def asks_if_it_helped(conn: sqlite3.Connection, log: sqlite3.Row) -> bool:
    """Only as-needed medicine is taken to relieve something, so only it gets "Did it help?".

    Scheduled prevention (anti-nausea, steroids), white-cell boosters, cancer pills and clinic doses can't be
    judged from one dose. Doses logged at another time are stored as kind 'as_needed' whatever the medicine
    is, so look at the plan: the dose's own plan if it has one, otherwise whether the medicine has an
    as-needed plan at all.
    """
    if log["kind"] != "as_needed":
        return False
    if log["plan_id"]:
        row = conn.execute("SELECT kind FROM plans WHERE id = ?", (log["plan_id"],)).fetchone()
        if row is not None and not log["slot"]:
            return True  # "Took one": an as-needed dose, even if its plan was changed to a schedule later
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
    """Paper-sheet style grid: one row per (medicine, kind), one column per cycle day.

    A logged dose sits under the planned dose it counts for, so a 9 PM dose taken at 12:30 AM stays on its own day
    and a "Log a dose" entry that filled a scheduled dose sits in the Scheduled row. Doses that fit no planned dose
    sit on the day they were taken, in a row for that medicine. A row is added for a medicine that has doses here
    but no schedule in this cycle any more (moved to later cycles, or stopped), so a given dose never vanishes.
    """
    idx = _index(conn)
    span = cycle_span(conn, cycle)
    days = list(range(0, span + 1))
    rows: dict[tuple[int, str], GridRow] = {}

    def row_for(mid: int, kind: str, name: str, detail: str) -> GridRow:
        if (mid, kind) not in rows:
            rows[(mid, kind)] = GridRow(mid, name, kind, detail, {d: GridCell() for d in days})
        return rows[(mid, kind)]

    def detail_of(plan: sqlite3.Row) -> str:
        return " · ".join(x for x in (plan["med_strength"], plan["dose"] or plan["med_dose"]) if x)

    nd1 = _next_day1(idx.cycles, cycle)
    for plan in plans_for(conn, cycle):
        r = row_for(plan["medication_id"], plan["kind"], plan["med_name"], detail_of(plan))
        if plan["kind"] == "as_needed":
            for dn in range(plan["start_day"], plan_end(plan, cycle, nd1) + 1):
                if dn in r.cells:
                    r.cells[dn].allowed = True
    for s in slots(conn, cycle):
        if s.day not in days:
            continue
        cell = row_for(s.plan["medication_id"], s.kind, s.plan["med_name"], detail_of(s.plan)).cells[s.day]
        cell.planned += 1
        if s.time:
            cell.times.append(s.time)
        if s.log is None:
            if s.status(now) in ("missed", "late", "not_logged"):
                cell.missing += 1
            else:
                cell.upcoming += 1
        else:
            (cell.taken if s.log["status"] == "taken" else cell.skipped).append(s.log)
    start, end = date_of(cycle, 0), date_of(cycle, span)
    for log in idx.logs:
        if log["id"] in idx.paired or log["status"] not in ("taken", "skipped"):
            continue
        d = home_date(log)
        if d is None or not (start <= d <= end):
            continue
        owner = None  # a date shared with the next cycle (its Day 0) is shown on that cycle's sheet
        for c in idx.cycles:
            if d >= day1_of(c) - timedelta(days=1):
                owner = c
        if owner is None or owner["id"] != cycle["id"]:
            continue
        mid = log["medication_id"]
        kind = log["kind"] if log["kind"] in ("clinic", "scheduled", "as_needed") else "as_needed"
        row = rows.get((mid, kind)) or next((r for (m, _), r in rows.items() if m == mid), None)
        if row is None:
            row = row_for(mid, kind, log["med_name"], "Logged, not on this cycle's schedule")
        cell = row.cells[day_number(cycle, d)]
        (cell.taken if log["status"] == "taken" else cell.skipped).append(log)
    for r in rows.values():
        for cell in r.cells.values():
            cell.taken.sort(key=lambda log: log["taken_at"] or "")
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
    med_rows = sorted(rows.values(), key=lambda r: (order.get(r.kind, 3), r.name))
    symptom_order = {s.key: i for i, s in enumerate(sx.SYMPTOMS)}
    return days, med_rows, sorted(sym_rows.values(), key=lambda r: (symptom_order.get(r.symptom, 99), r.name))
