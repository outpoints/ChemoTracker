"""Change history: turn the rows the audit triggers write into plain sentences.

Each event says who did it, when, and what changed ("Did it help: not answered -> Yes").
Deleted doses and symptom entries can be put back from their last snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import symptoms as sx
from .db import NOT_CONTENT, rowid

ENTITY_NAMES = {
    "dose": "Dose", "symptom": "Symptom", "medication": "Medicine", "plan": "Schedule",
    "cycle": "Cycle", "person": "Person", "setting": "Setting", "database": "Whole record",
}

LABELS = {
    "dose": {"status": "Status", "taken_at": "Time", "dose": "Dose", "reason": "Reason", "helped": "Did it help",
             "dose_feel": "How the dose felt", "side_effects": "Side effects", "notes": "Notes", "slot": "Planned for",
             "medication_id": "Medicine", "plan_id": "Schedule", "kind": "Kind"},
    "symptom": {"symptom": "Symptom", "label": "Name", "severity": "How bad", "value": "Number", "notes": "Notes",
                "logged_at": "Time"},
    "medication": {"name": "Name", "brand": "Brand", "strength": "Strength", "dose": "Dose", "directions": "Label",
                   "route": "How it goes in", "min_hours_between": "Hours between doses", "max_per_day": "Most a day",
                   "notes": "Notes", "active": "On the list", "category": "Group", "info_key": "Catalog entry"},
    "plan": {"kind": "Kind", "start_day": "From day", "end_day": "To day", "times": "Times", "label": "When",
             "from_cycle": "From cycle", "to_cycle": "To cycle", "dose": "Dose", "medication_id": "Medicine"},
    "cycle": {"number": "Number", "day1": "Chemo day (Day 1)", "length_days": "Days", "regimen": "Regimen",
              "notes": "Notes"},
    "person": {"name": "Name", "role": "Role", "active": "Active", "color": "Color"},
    "setting": {"value": "Value"},
}

SETTING_NAMES = {
    "patient_name": "Name on the report", "clinic_name": "Clinic", "clinic_phone": "Clinic phone",
    "after_hours_phone": "After-hours phone", "fever_f": "Fever line (°F)", "temp_unit": "Temperature unit",
    "call_if": "When to call", "onboarded": "Setup finished", "ntfy_enabled": "Reminders",
    "ntfy_server": "Reminder server", "ntfy_topic": "Reminder topic", "ntfy_token": "Reminder access token",
    "ntfy_lead": "Remind ahead", "ntfy_nag": "Remind again if not marked",
    "ntfy_chemo": "Chemo-tomorrow reminder", "ntfy_names": "Medicine names in reminders", "app_url": "Link address",
}

HELPED = {"yes": "Yes", "some": "A little", "no": "No", "unsure": "Not sure"}
FEEL = {"right": "About right", "too_strong": "Too strong", "not_enough": "Not enough"}
SEVERITY = {0: "None", 1: "Mild", 2: "Moderate", 3: "Severe"}
SKIP_FIELDS = NOT_CONTENT | {"id"}
CAN_PUT_BACK = {"dose": "dose_logs", "symptom": "symptom_logs"}


@dataclass
class Event:
    id: int
    at: datetime
    who: str
    entity: str
    ref: str | None
    action: str
    subject: str
    changes: list[tuple[str, str, str]] = field(default_factory=list)  # (field, before, after)
    snapshot: dict = field(default_factory=dict)
    can_put_back: bool = False

    @property
    def verb(self) -> str:
        return {"created": "added", "changed": "changed", "deleted": "deleted", "restored": "restored"}[self.action]


def _fmt_time(value: str) -> str:
    from .schedule import fmt_time

    try:
        dt = datetime.fromisoformat(value.replace("T", " ")[:16])
    except ValueError:
        return value
    return f"{dt:%b} {dt.day}, {fmt_time(dt)}"


def _value(entity: str, key: str, value, names: "_Names", at: int | None = None) -> str:
    if value is None or value == "":
        return "blank"
    if entity == "dose":
        if key == "helped":
            return HELPED.get(value, str(value))
        if key == "dose_feel":
            return FEEL.get(value, str(value))
        if key == "status":
            return {"taken": "Taken", "skipped": "Skipped"}.get(value, str(value))
        if key == "taken_at":
            return _fmt_time(value)
        if key == "medication_id":
            return names.medicine(value, at) or f"#{value}"
    if entity == "symptom":
        if key == "severity":
            return SEVERITY.get(value, str(value))
        if key == "symptom":
            return sx.lookup(value).name if sx.lookup(value) else value
        if key == "logged_at":
            return _fmt_time(value)
    if key == "active":
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _snap_name(text: str | None) -> str | None:
    try:
        return json.loads(text)["name"] if text else None
    except (TypeError, ValueError, KeyError):
        return None


class _Names:
    """A medicine's name as it was when each event happened.

    Before v5 SQLite could give a new medicine the id of a deleted one, so an id alone can belong to two
    medicines over time. Each medicine's own history (added, renamed, deleted) says which name the id had
    at any moment, so a deleted medicine's doses never take the name of the medicine added after it.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.current = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM medications")}
        self.timeline: dict[int, list[tuple[int, str | None, str | None]]] = {}
        for r in conn.execute("SELECT id, ref, before, after FROM history WHERE entity = 'medication' ORDER BY id"):
            try:
                ref = int(r["ref"])
            except (TypeError, ValueError):
                continue
            self.timeline.setdefault(ref, []).append((r["id"], _snap_name(r["before"]), _snap_name(r["after"])))

    def medicine(self, med_id: Any, at: int | None = None) -> str | None:
        """The name medicine `med_id` had at history event `at` (or now, or when it was deleted)."""
        try:
            med_id = int(med_id)
        except (TypeError, ValueError):
            return None
        events = self.timeline.get(med_id, [])
        if at is not None and events:
            earlier = [e for e in events if e[0] <= at]
            if earlier and (earlier[-1][2] or earlier[-1][1]):
                return earlier[-1][2] or earlier[-1][1]
            later = [e for e in events if e[0] > at]
            if later and later[0][1]:
                return later[0][1]  # added before history began: its first change says what it was called
        if med_id in self.current:
            return self.current[med_id]
        for _, before, after in reversed(events):
            if after or before:
                return after or before
        return None


def _names(conn: sqlite3.Connection) -> _Names:
    return _Names(conn)


def _subject(entity: str, snap: dict, names: _Names, ref: str | None, at: int | None = None) -> str:
    if entity == "dose":
        return names.medicine(snap.get("medication_id"), at) or "A medicine"
    if entity == "symptom":
        s = sx.lookup(snap.get("symptom") or "")
        return snap.get("label") or (s.name if s else "A symptom")
    if entity == "medication":
        return snap.get("name") or "A medicine"
    if entity == "plan":
        return f"{names.medicine(snap.get('medication_id'), at) or 'A medicine'} schedule"
    if entity == "cycle":
        return f"Cycle {snap.get('number', '')}".strip()
    if entity == "person":
        return snap.get("name") or "A person"
    if entity == "setting":
        return SETTING_NAMES.get(ref or "", ref or "A setting")
    if entity == "database":
        return "Whole record"
    return ENTITY_NAMES.get(entity, entity)


def _event(row: sqlite3.Row, people: dict[int, str], names: _Names, tz: ZoneInfo,
           existing: dict[tuple[str, str], tuple]) -> Event:
    before = json.loads(row["before"]) if row["before"] else {}
    after = json.loads(row["after"]) if row["after"] else {}
    snap = after or before
    entity = row["entity"]
    changes: list[tuple[str, str, str]] = []
    labels = LABELS.get(entity, {})
    if row["action"] == "changed":
        for key in sorted(set(before) | set(after), key=lambda k: list(labels).index(k) if k in labels else 99):
            if key in SKIP_FIELDS or before.get(key) == after.get(key):
                continue
            if entity == "setting" and key == "key":
                continue
            changes.append((str(labels.get(key, key.replace("_", " ").capitalize())),
                            _value(entity, key, before.get(key), names, row["id"]),
                            _value(entity, key, after.get(key), names, row["id"])))
    elif row["action"] == "created" and entity in ("dose", "symptom"):
        for key in labels:
            if key in ("medication_id", "plan_id", "kind", "symptom", "slot") or snap.get(key) in (None, ""):
                continue
            changes.append((labels[key], "", _value(entity, key, snap.get(key), names, row["id"])))
    elif row["action"] == "restored" and entity == "database":
        changes.append(("From backup", "", row["ref"] or ""))
    at = datetime.fromisoformat(row["at"]).astimezone(tz).replace(tzinfo=None)
    if entity == "setting":
        snap = {**after, "_before": before.get("value")} if after else {**before, "_before": before.get("value")}
    return Event(
        id=row["id"], at=at, who=people.get(row["actor"], "Someone") if row["actor"] else "Someone",
        entity=entity, ref=row["ref"], action=row["action"],
        subject=_subject(entity, snap, names, row["ref"], row["id"]),
        changes=changes, snapshot=before if row["action"] == "deleted" else snap,
        can_put_back=row["action"] == "deleted" and entity in CAN_PUT_BACK
        and existing.get((entity, row["ref"])) != _identity(entity, before),
    )


def _identity(entity: str, snap: dict | sqlite3.Row) -> tuple:
    """What makes an entry the same entry. Not its id alone: before v5 a deleted entry's id could be reused."""
    keys = ("created_at", "medication_id") if entity == "dose" else ("created_at", "symptom")
    return tuple(snap[k] if k in snap.keys() else None for k in keys)


def _existing(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple]:
    out = {("dose", str(r["id"])): _identity("dose", r) for r in conn.execute("SELECT id, created_at, medication_id FROM dose_logs")}
    out |= {("symptom", str(r["id"])): _identity("symptom", r) for r in conn.execute("SELECT id, created_at, symptom FROM symptom_logs")}
    return out


def events(conn: sqlite3.Connection, tz: ZoneInfo, entity: str | None = None, ref: str | int | None = None,
           limit: int = 100, offset: int = 0, since: str | None = None, until: str | None = None) -> list[Event]:
    """Newest first. `since`/`until` are UTC ISO strings."""
    where, args = [], []
    if entity:
        where.append("entity = ?")
        args.append(entity)
    if ref is not None:
        where.append("ref = ?")
        args.append(str(ref))
    if since:
        where.append("at >= ?")
        args.append(since)
    if until:
        where.append("at < ?")
        args.append(until)
    sql = "SELECT * FROM history" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ? OFFSET ?"
    return _build(conn, tz, conn.execute(sql, [*args, limit, offset]).fetchall())


def for_medicine(conn: sqlite3.Connection, tz: ZoneInfo, med_id: int, limit: int = 40) -> list[Event]:
    """The medicine's label and schedule changes (not every dose)."""
    rows = conn.execute(
        """SELECT * FROM history WHERE (entity = 'medication' AND ref = ?)
              OR (entity = 'plan' AND json_extract(COALESCE(after, before), '$.medication_id') = ?)
            ORDER BY id DESC LIMIT ?""",
        (str(med_id), med_id, limit),
    ).fetchall()
    return _build(conn, tz, rows)


def _build(conn: sqlite3.Connection, tz: ZoneInfo, rows: list[sqlite3.Row]) -> list[Event]:
    if not rows:
        return []
    people = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM profiles")}
    names = _names(conn)
    existing = _existing(conn)
    return _group_settings([_event(r, people, names, tz, existing) for r in rows])


def _setting_value(key: str, value) -> str:
    if value in (None, ""):
        return "Off" if key in ("ntfy_enabled", "ntfy_names", "ntfy_chemo") else "blank"
    if key in ("ntfy_enabled", "ntfy_names", "ntfy_chemo", "onboarded"):
        return "On" if str(value) == "1" or key == "onboarded" else "Off"
    if key in ("ntfy_lead", "ntfy_nag"):
        return "Off" if str(value) == "0" and key == "ntfy_nag" else ("At the time" if str(value) == "0" else f"{value} minutes")
    return str(value)


def _group_settings(events: list[Event]) -> list[Event]:
    """One Settings save is one event ("Jane changed reminder settings"), not a line per field."""
    out: list[Event] = []
    for e in events:
        if e.entity != "setting":
            out.append(e)
            continue
        key = e.ref or ""
        before = _setting_value(key, e.snapshot.get("_before")) if e.action == "changed" else ""
        line = (SETTING_NAMES.get(key, key), before, _setting_value(key, e.snapshot.get("value")))
        prev = out[-1] if out else None
        if prev and prev.entity == "setting" and prev.who == e.who and abs((prev.at - e.at).total_seconds()) <= 2:
            prev.changes.append(line)
            if not (key.startswith("ntfy_") or key == "app_url"):
                prev.subject = "Settings"
            continue
        group = Event(id=e.id, at=e.at, who=e.who, entity="setting", ref=key, action="changed",
                      subject="Reminder settings" if key.startswith("ntfy_") or key == "app_url" else "Settings",
                      changes=[line])
        out.append(group)
    return out


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]


class CannotPutBack(Exception):
    pass


class CheckFirst(CannotPutBack):
    """Putting this dose back could record one dose twice. The person decides, with the warning in front of them."""

    def __init__(self, warning: str, med: sqlite3.Row, when: datetime) -> None:
        super().__init__(warning)
        self.warning, self.med, self.when = warning, med, when


def put_back(conn: sqlite3.Connection, event_id: int, confirm: bool = False, by: int | None = None,
             now: datetime | None = None) -> tuple[str, int]:
    """Re-create a deleted dose or symptom entry from its last snapshot, with its original id when it's free.

    A dose that was logged again since it was deleted (from its row, from "Log a dose", or after its time
    changed) is not brought back a second time: the same pairing every page uses decides whether its planned
    dose is already filled, and the same double-dose check every logging route runs asks first (CheckFirst)
    unless `confirm` says the person has seen it.
    """
    row = conn.execute("SELECT * FROM history WHERE id = ?", (event_id,)).fetchone()
    if row is None or row["action"] != "deleted" or row["entity"] not in CAN_PUT_BACK or not row["before"]:
        raise CannotPutBack("That entry can't be put back.")
    table = CAN_PUT_BACK[row["entity"]]
    snap = json.loads(row["before"])
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    data = {c: snap.get(c) for c in cols if c in snap}
    holder = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (data.get("id"),)).fetchone()
    if holder is not None:
        if _identity(row["entity"], holder) == _identity(row["entity"], snap):
            raise CannotPutBack("It's already back.")
        data.pop("id")  # its old id now belongs to another entry, so it comes back with a new one
    if table == "dose_logs":
        _check_dose(conn, data, confirm, by, now)
    for who in ("logged_by", "updated_by"):
        if data.get(who) and not conn.execute("SELECT 1 FROM profiles WHERE id = ?", (data[who],)).fetchone():
            data[who] = None
    keys = list(data)
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                       [data[k] for k in keys])
    return row["entity"], rowid(cur)


def _check_dose(conn: sqlite3.Connection, data: dict, confirm: bool, by: int | None, now: datetime | None) -> None:
    from . import schedule

    med = conn.execute("SELECT * FROM medications WHERE id = ?", (data.get("medication_id"),)).fetchone()
    if med is None:
        raise CannotPutBack("Its medicine was removed, so this dose can't come back on its own.")
    if data.get("plan_id") and not conn.execute("SELECT 1 FROM plans WHERE id = ?", (data["plan_id"],)).fetchone():
        data["plan_id"] = None
    slot = None
    if data.get("plan_id") and data.get("slot"):
        if conn.execute("SELECT 1 FROM dose_logs WHERE plan_id = ? AND slot = ?", (data["plan_id"], data["slot"])).fetchone():
            raise CannotPutBack("That planned dose has a newer entry. Change that one instead.")
        slot = schedule.find_slot(conn, data["plan_id"], data["slot"])
        if slot is not None and slot.log is not None:
            # Logged again some other way ("Log a dose", Took one, or before a time change): the row is filled.
            raise CannotPutBack("That planned dose has a newer entry. Change that one instead.")
    if data.get("status") != "taken" or confirm:
        return
    when = schedule.parse_local(data.get("taken_at")) or (slot.dt if slot is not None else None)
    if when is None:
        return
    warning = schedule.dose_warning(conn, med, when, by=by, slot=slot, now=now)
    if warning:
        raise CheckFirst(warning, med, when)
