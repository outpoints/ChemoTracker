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


def _value(entity: str, key: str, value, names: dict[tuple[str, Any], str]) -> str:
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
            return names.get(("medication", value), f"#{value}")
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


def _names(conn: sqlite3.Connection) -> dict[tuple[str, Any], str]:
    out: dict[tuple[str, Any], str] = {("medication", r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM medications")}
    # Medicines that were deleted keep their name in their last snapshot.
    for r in conn.execute("SELECT ref, before FROM history WHERE entity = 'medication' AND action = 'deleted'"):
        try:
            out.setdefault(("medication", int(r["ref"])), json.loads(r["before"])["name"])
        except (TypeError, ValueError, KeyError):
            pass
    return out


def _subject(entity: str, snap: dict, names: dict[tuple[str, Any], str], ref: str | None) -> str:
    if entity == "dose":
        return names.get(("medication", snap.get("medication_id")), "A medicine")
    if entity == "symptom":
        s = sx.lookup(snap.get("symptom") or "")
        return snap.get("label") or (s.name if s else "A symptom")
    if entity == "medication":
        return snap.get("name") or "A medicine"
    if entity == "plan":
        return f"{names.get(('medication', snap.get('medication_id')), 'A medicine')} schedule"
    if entity == "cycle":
        return f"Cycle {snap.get('number', '')}".strip()
    if entity == "person":
        return snap.get("name") or "A person"
    if entity == "setting":
        return SETTING_NAMES.get(ref or "", ref or "A setting")
    if entity == "database":
        return "Whole record"
    return ENTITY_NAMES.get(entity, entity)


def _event(row: sqlite3.Row, people: dict[int, str], names: dict, tz: ZoneInfo, existing: set[tuple[str, str]]) -> Event:
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
                            _value(entity, key, before.get(key), names), _value(entity, key, after.get(key), names)))
    elif row["action"] == "created" and entity in ("dose", "symptom"):
        for key in labels:
            if key in ("medication_id", "plan_id", "kind", "symptom", "slot") or snap.get(key) in (None, ""):
                continue
            changes.append((labels[key], "", _value(entity, key, snap.get(key), names)))
    elif row["action"] == "restored" and entity == "database":
        changes.append(("From backup", "", row["ref"] or ""))
    at = datetime.fromisoformat(row["at"]).astimezone(tz).replace(tzinfo=None)
    if entity == "setting":
        snap = {**after, "_before": before.get("value")} if after else {**before, "_before": before.get("value")}
    return Event(
        id=row["id"], at=at, who=people.get(row["actor"], "Someone") if row["actor"] else "Someone",
        entity=entity, ref=row["ref"], action=row["action"], subject=_subject(entity, snap, names, row["ref"]),
        changes=changes, snapshot=before if row["action"] == "deleted" else snap,
        can_put_back=row["action"] == "deleted" and entity in CAN_PUT_BACK and (entity, row["ref"]) not in existing,
    )


def _existing(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    out = {("dose", str(r[0])) for r in conn.execute("SELECT id FROM dose_logs")}
    out |= {("symptom", str(r[0])) for r in conn.execute("SELECT id FROM symptom_logs")}
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


def put_back(conn: sqlite3.Connection, event_id: int) -> tuple[str, int]:
    """Re-create a deleted dose or symptom entry from its last snapshot, with its original id."""
    row = conn.execute("SELECT * FROM history WHERE id = ?", (event_id,)).fetchone()
    if row is None or row["action"] != "deleted" or row["entity"] not in CAN_PUT_BACK or not row["before"]:
        raise CannotPutBack("That entry can't be put back.")
    table = CAN_PUT_BACK[row["entity"]]
    snap = json.loads(row["before"])
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    data = {c: snap.get(c) for c in cols if c in snap}
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (data.get("id"),)).fetchone():
        raise CannotPutBack("It's already back.")
    if table == "dose_logs":
        if data.get("medication_id") and not conn.execute(
                "SELECT 1 FROM medications WHERE id = ?", (data["medication_id"],)).fetchone():
            raise CannotPutBack("Its medicine was removed, so this dose can't come back on its own.")
        if data.get("plan_id") and not conn.execute("SELECT 1 FROM plans WHERE id = ?", (data["plan_id"],)).fetchone():
            data["plan_id"] = None
        if data.get("plan_id") and data.get("slot") and conn.execute(
                "SELECT 1 FROM dose_logs WHERE plan_id = ? AND slot = ?", (data["plan_id"], data["slot"])).fetchone():
            raise CannotPutBack("That planned dose has a newer entry. Change that one instead.")
    for who in ("logged_by", "updated_by"):
        if data.get(who) and not conn.execute("SELECT 1 FROM profiles WHERE id = ?", (data[who],)).fetchone():
            data[who] = None
    keys = list(data)
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                       [data[k] for k in keys])
    return row["entity"], rowid(cur)
