"""Reads and writes. Every function takes an open connection; callers commit."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from . import medinfo, units
from . import symptoms as sx
from .db import atomic, get_settings, rowid, utcnow

ROUTES = tuple(medinfo.ROUTE_NAMES)
KINDS = tuple(medinfo.KIND_NAMES)
_TIME = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


class InvalidInput(ValueError):
    """A message safe to show to the person filling in the form."""


def clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_times(values: list[str] | str | None) -> list[str]:
    if not values:
        return []
    parts = values if isinstance(values, list) else re.split(r"[,\s]+", values)
    out = []
    for raw in parts:
        if not raw or not raw.strip():
            continue
        m = _TIME.match(raw)
        if not m:
            raise InvalidInput(f"“{raw}” isn't a time. Use the time picker, for example 9:00 PM.")
        out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(set(out))


def parse_float(value: str | None, what: str, low: float | None = None, high: float | None = None) -> float | None:
    value = clean(value)
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        raise InvalidInput(f"{what} needs to be a number.") from None
    if (low is not None and number < low) or (high is not None and number > high):
        raise InvalidInput(f"{what} should be between {low:g} and {high:g}.")
    return number


def parse_int(value: str | None, what: str, low: int | None = None, high: int | None = None) -> int | None:
    number = parse_float(value, what, low, high)
    if number is None:
        return None
    if number != int(number):
        raise InvalidInput(f"{what} needs to be a whole number.")
    return int(number)


def parse_local_dt(value: str | None, fallback: datetime) -> datetime:
    value = clean(value)
    if value is None:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("T", " ")).replace(second=0, microsecond=0, tzinfo=None)
    except ValueError:
        raise InvalidInput("That date and time doesn't look right.") from None


# ---------------------------------------------------------------- medications


def medications(conn: sqlite3.Connection, active: bool | None = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM medications"
    if active is not None:
        sql += f" WHERE active = {1 if active else 0}"
    return conn.execute(sql + " ORDER BY lower(name)").fetchall()


def medication(conn: sqlite3.Connection, med_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()


MED_FIELDS = ("name", "brand", "info_key", "category", "route", "strength", "dose", "directions",
              "min_hours_between", "max_per_day", "notes", "prep_minutes", "prep_note")


def validate_med(values: dict) -> dict:
    out = {k: values.get(k) for k in MED_FIELDS if k in values}
    for k in ("name", "brand", "strength", "dose", "directions", "notes", "info_key", "category"):
        if k in out:
            out[k] = clean(out[k])
    if "name" in out and not out["name"]:
        raise InvalidInput("Every medicine needs a name.")
    if "route" in out and out["route"] not in ROUTES:
        out["route"] = "oral"
    if "min_hours_between" in out:
        out["min_hours_between"] = parse_float(_s(out["min_hours_between"]), "Hours between doses", 0.25, 168)
    if "max_per_day" in out:
        out["max_per_day"] = parse_int(_s(out["max_per_day"]), "Most doses in a day", 1, 48)
    if "prep_minutes" in out:
        out["prep_minutes"] = parse_int(_s(out["prep_minutes"]), "Minutes before", 1, 1440)
    if "prep_note" in out:
        out["prep_note"] = clean(out["prep_note"])
    if "prep_minutes" in out or "prep_note" in out:
        if out.get("prep_minutes") and not out.get("prep_note"):
            out["prep_note"] = "Get it ready"
        if out.get("prep_note") and not out.get("prep_minutes"):
            raise InvalidInput("Say how many minutes before the dose to get it ready, or clear what to do.")
    if "info_key" in out and out["info_key"] and medinfo.lookup(out["info_key"]) is None:
        out["info_key"] = None
    return out


def _s(value) -> str | None:
    return None if value is None else str(value)


def create_medication(conn: sqlite3.Connection, values: dict) -> int:
    data = validate_med(values)
    data.setdefault("route", "oral")
    cols = list(data) + ["created_at", "updated_at"]
    now = utcnow()
    cur = conn.execute(
        f"INSERT INTO medications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [*data.values(), now, now],
    )
    return rowid(cur)


def update_medication(conn: sqlite3.Connection, med_id: int, values: dict) -> None:
    data = validate_med(values)
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE medications SET {sets}, updated_at = ? WHERE id = ?", [*data.values(), utcnow(), med_id])


def set_active(conn: sqlite3.Connection, med_id: int, active: bool) -> None:
    conn.execute("UPDATE medications SET active = ?, updated_at = ? WHERE id = ?", (1 if active else 0, utcnow(), med_id))


def delete_medication(conn: sqlite3.Connection, med_id: int) -> None:
    conn.execute("DELETE FROM medications WHERE id = ?", (med_id,))


def add_from_catalog(conn: sqlite3.Connection, key: str) -> int:
    """Create a medicine (or reactivate the existing one) with a starter plan from the catalog."""
    info = medinfo.lookup(key)
    if info is None:
        raise InvalidInput("That medicine isn't in the list.")
    existing = conn.execute("SELECT id FROM medications WHERE info_key = ?", (info.key,)).fetchone()
    if existing:
        set_active(conn, existing["id"], True)
        return existing["id"]
    med_id = create_medication(
        conn,
        {
            "name": info.generic,
            "brand": info.brands[0] if info.brands else None,
            "info_key": info.key,
            "category": info.category,
            "route": info.defaults.route,
            **({"prep_minutes": str(info.prep[0]), "prep_note": info.prep[1]} if info.prep else {}),
        },
    )
    d = info.defaults
    create_plan(
        conn,
        med_id,
        {
            "kind": d.kind,
            "start_day": str(d.days[0]),
            "end_day": "" if d.days[1] == 0 else str(d.days[1]),
            "times": list(d.times),
            "label": d.label,
        },
    )
    return med_id


# ---------------------------------------------------------------- plans


def plans(conn: sqlite3.Connection, med_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM plans WHERE medication_id = ? ORDER BY from_cycle, start_day, id", (med_id,)).fetchall()


def plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()


def validate_plan(values: dict) -> dict:
    kind = values.get("kind") or "scheduled"
    if kind not in KINDS:
        raise InvalidInput("Choose how this medicine is taken.")
    days = values.get("days")
    if isinstance(days, str) and days.strip():
        days = [days]
    if values.get("whole_cycle") and not days and values.get("start_day") in (None, ""):
        days = ["1"]
    if isinstance(days, list) and days:
        picked = sorted({int(d) for d in days if str(d).strip().isdigit()})
        if not picked:
            raise InvalidInput("Pick at least one day.")
        if picked != list(range(picked[0], picked[-1] + 1)):
            raise InvalidInput("Pick days that are next to each other, like Days 2, 3 and 4. Add a second schedule for a separate stretch.")
        start, end = picked[0], picked[-1]
    else:
        start = parse_int(_s(values.get("start_day")), "First day", 0, 90)
        end = parse_int(_s(values.get("end_day")), "Last day", 0, 90)
        if start is None:
            raise InvalidInput("Pick the first day of the cycle this medicine is used.")
    if values.get("whole_cycle"):
        end = None
    if end is not None and end < start:
        raise InvalidInput("The last day can't be before the first day.")
    times = parse_times(values.get("times"))
    if kind == "scheduled" and not times:
        raise InvalidInput("Add at least one time for a scheduled medicine.")
    if kind != "scheduled":
        times = []
    from_cycle = parse_int(_s(values.get("from_cycle")), "First cycle", 1, 99) or 1
    to_cycle = parse_int(_s(values.get("to_cycle")), "Last cycle", 1, 99)
    if to_cycle is not None and to_cycle < from_cycle:
        raise InvalidInput("The last cycle can't be before the first cycle.")
    return {
        "kind": kind,
        "start_day": start,
        "end_day": end,
        "times": ",".join(times),
        "label": clean(values.get("label")),
        "dose": clean(values.get("dose")),
        "from_cycle": from_cycle,
        "to_cycle": to_cycle,
    }


def create_plan(conn: sqlite3.Connection, med_id: int, values: dict) -> int:
    data = validate_plan(values)
    cur = conn.execute(
        """INSERT INTO plans (medication_id, kind, start_day, end_day, times, label, dose, from_cycle, to_cycle, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (med_id, data["kind"], data["start_day"], data["end_day"], data["times"], data["label"], data["dose"],
         data["from_cycle"], data["to_cycle"], utcnow()),
    )
    return rowid(cur)


def update_plan(conn: sqlite3.Connection, plan_id: int, values: dict) -> None:
    data = validate_plan(values)
    conn.execute(
        """UPDATE plans SET kind = ?, start_day = ?, end_day = ?, times = ?, label = ?, dose = ?, from_cycle = ?,
                  to_cycle = ? WHERE id = ?""",
        (data["kind"], data["start_day"], data["end_day"], data["times"], data["label"], data["dose"],
         data["from_cycle"], data["to_cycle"], plan_id),
    )


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> None:
    conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))


# ---------------------------------------------------------------- cycles


def cycle(conn: sqlite3.Connection, cycle_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()


def validate_cycle(values: dict) -> dict:
    number = parse_int(_s(values.get("number")), "Cycle number", 1, 99)
    if number is None:
        raise InvalidInput("Enter the cycle number.")
    try:
        day1 = date.fromisoformat((values.get("day1") or "").strip())
    except ValueError:
        raise InvalidInput("Pick the date of chemo day (Day 1).") from None
    length = parse_int(_s(values.get("length_days")), "Days in the cycle", 1, 90) or 14
    return {"number": number, "day1": day1.isoformat(), "length_days": length,
            "regimen": clean(values.get("regimen")), "notes": clean(values.get("notes"))}


def create_cycle(conn: sqlite3.Connection, values: dict) -> int:
    data = validate_cycle(values)
    try:
        cur = conn.execute(
            "INSERT INTO cycles (number, day1, length_days, regimen, notes, created_at) VALUES (?,?,?,?,?,?)",
            (data["number"], data["day1"], data["length_days"], data["regimen"], data["notes"], utcnow()),
        )
    except sqlite3.IntegrityError:
        raise InvalidInput(f"There is already a cycle {data['number']}.") from None
    return rowid(cur)


def create_cycle_series(conn: sqlite3.Connection, values: dict) -> list[int]:
    """Cycles first..first+count-1, each starting `length_days` after the one before.

    This is how regimens are written ("every 14 days x 4 cycles"). Dates stay
    editable per cycle because treatment is often delayed.
    """
    data = validate_cycle(values)
    count = parse_int(_s(values.get("count")), "How many cycles", 1, 60) or 1
    first = date.fromisoformat(data["day1"])
    ids = []
    with atomic(conn):  # all cycles or none, e.g. if cycle 3 already exists
        for i in range(count):
            ids.append(create_cycle(conn, {**data, "number": data["number"] + i,
                                           "day1": (first + timedelta(days=i * data["length_days"])).isoformat()}))
    return ids


def update_cycle(conn: sqlite3.Connection, cycle_id: int, values: dict) -> None:
    data = validate_cycle(values)
    try:
        conn.execute(
            "UPDATE cycles SET number = ?, day1 = ?, length_days = ?, regimen = ?, notes = ? WHERE id = ?",
            (data["number"], data["day1"], data["length_days"], data["regimen"], data["notes"], cycle_id),
        )
    except sqlite3.IntegrityError:
        raise InvalidInput(f"There is already a cycle {data['number']}.") from None


def shift_later_cycles(conn: sqlite3.Connection, number: int, days: int) -> int:
    """Move every cycle after `number` by `days` (chemo delayed or brought forward)."""
    if not days:
        return 0
    later = conn.execute("SELECT id, day1 FROM cycles WHERE number > ? ORDER BY number", (number,)).fetchall()
    for row in later:
        new_day1 = (date.fromisoformat(row["day1"]) + timedelta(days=days)).isoformat()
        conn.execute("UPDATE cycles SET day1 = ? WHERE id = ?", (new_day1, row["id"]))
    return len(later)


def delete_cycle(conn: sqlite3.Connection, cycle_id: int) -> None:
    conn.execute("DELETE FROM cycles WHERE id = ?", (cycle_id,))


def next_cycle_defaults(conn: sqlite3.Connection, today: date) -> dict:
    last = conn.execute("SELECT * FROM cycles ORDER BY number DESC LIMIT 1").fetchone()
    if last is None:
        return {"number": 1, "day1": today.isoformat(), "length_days": 14, "regimen": ""}
    day1 = date.fromisoformat(last["day1"]) + timedelta(days=last["length_days"])
    return {"number": last["number"] + 1, "day1": day1.isoformat(), "length_days": last["length_days"],
            "regimen": last["regimen"] or ""}


# ---------------------------------------------------------------- prescriptions


def import_parsed(conn: sqlite3.Connection, med, cycles: tuple[int, int | None] | None = None,
                  into: int | None = None) -> int:
    """Create (or fill in) a medicine from a parsed prescription. `med` is an rxparse.ParsedMed."""
    from . import rxparse

    if into is not None:
        med_id = into
    elif med.info_key:
        med_id = add_from_catalog(conn, med.info_key)
    else:
        existing = conn.execute("SELECT id FROM medications WHERE lower(name) = lower(?) AND info_key IS NULL",
                                (med.name,)).fetchone()
        med_id = existing["id"] if existing else create_medication(
            conn, {"name": med.name or "Medicine", "route": med.route, "category": "custom"})
        set_active(conn, med_id, True)
    values = {"strength": med.strength, "dose": med.dose, "directions": med.directions,
              "min_hours_between": med.min_hours_between, "max_per_day": med.max_per_day, "route": med.route}
    if med.prep_minutes:
        values["prep_minutes"], values["prep_note"] = str(med.prep_minutes), med.prep_note
    if med.brand:
        values["brand"] = med.brand
    update_medication(conn, med_id, values)
    if med.plans:
        for p in plans(conn, med_id):
            delete_plan(conn, p["id"])
        for p in med.plans:
            values = rxparse.plan_values(p)
            if cycles:
                values["from_cycle"], values["to_cycle"] = str(cycles[0]), "" if cycles[1] is None else str(cycles[1])
            create_plan(conn, med_id, values)
    return med_id


# ---------------------------------------------------------------- dose logs


HELPED = {"yes": "Yes", "some": "A little", "no": "No", "unsure": "Not sure"}
DOSE_FEEL = {"right": "About right", "too_strong": "Too strong", "not_enough": "Not enough"}


def dose_log(conn: sqlite3.Connection, log_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT dl.*, m.name AS med_name, m.brand AS med_brand, m.info_key AS med_info,
                  m.strength AS med_strength, m.dose AS med_dose
             FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id WHERE dl.id = ?""",
        (log_id,),
    ).fetchone()


def slot_log(conn: sqlite3.Connection, plan_id: int, slot: str) -> sqlite3.Row | None:
    """The entry for one planned dose, with the name of whoever last touched it."""
    return conn.execute(
        """SELECT dl.*, p.name AS who FROM dose_logs dl
             LEFT JOIN profiles p ON p.id = COALESCE(dl.updated_by, dl.logged_by)
            WHERE dl.plan_id = ? AND dl.slot = ?""",
        (plan_id, slot),
    ).fetchone()


def log_slot(conn: sqlite3.Connection, plan_row: sqlite3.Row, slot: str, status: str, taken_at: datetime | None,
             dose: str | None, by: int | None = None) -> int:
    """Record a planned dose. Idempotent per (plan, slot): a second tap updates instead of duplicating."""
    if status not in ("taken", "skipped"):
        raise InvalidInput("Unknown status.")
    now = utcnow()
    taken = taken_at.strftime("%Y-%m-%d %H:%M") if (taken_at and status == "taken") else None
    existing = conn.execute("SELECT id FROM dose_logs WHERE plan_id = ? AND slot = ?", (plan_row["id"], slot)).fetchone()
    if existing:
        conn.execute(
            """UPDATE dose_logs SET status = ?, taken_at = ?, dose = COALESCE(?, dose), updated_at = ?,
                      updated_by = COALESCE(?, updated_by) WHERE id = ?""",
            (status, taken, dose, now, by, existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO dose_logs (medication_id, plan_id, kind, slot, status, taken_at, dose, logged_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (plan_row["medication_id"], plan_row["id"], plan_row["kind"], slot, status, taken, dose, by, now, now),
    )
    return rowid(cur)


def log_dose(conn: sqlite3.Connection, med_id: int, taken_at: datetime, dose: str | None, reason: str | None,
             plan_id: int | None = None, by: int | None = None) -> int:
    now = utcnow()
    cur = conn.execute(
        """INSERT INTO dose_logs (medication_id, plan_id, kind, slot, status, taken_at, dose, reason, logged_by,
                                  created_at, updated_at)
           VALUES (?,?,'as_needed',NULL,'taken',?,?,?,?,?,?)""",
        (med_id, plan_id, taken_at.strftime("%Y-%m-%d %H:%M"), clean(dose), clean(reason), by, now, now),
    )
    return rowid(cur)


def _first(value) -> str | None:
    """A single choice from a field that may arrive as a list (toggle chips submitted without scripts)."""
    if isinstance(value, list):
        value = next((v for v in value if v), None)
    return value or None


def update_dose_log(conn: sqlite3.Connection, log_id: int, values: dict, fallback: datetime, by: int | None = None) -> None:
    status = values.get("status") or "taken"
    if status not in ("taken", "skipped"):
        raise InvalidInput("Unknown status.")
    helped = _first(values.get("helped"))
    feel = _first(values.get("dose_feel"))
    if helped is not None and helped not in HELPED:
        helped = None
    if feel is not None and feel not in DOSE_FEEL:
        feel = None
    effects = values.get("side_effects")
    if isinstance(effects, list):
        effects = ", ".join(e.strip() for e in effects if e and e.strip())
    extra = clean(values.get("side_effects_other"))
    if extra:
        effects = f"{effects}, {extra}" if effects else extra
    taken = parse_local_dt(values.get("taken_at"), fallback) if status == "taken" else None
    conn.execute(
        """UPDATE dose_logs SET status = ?, taken_at = ?, dose = ?, reason = ?, helped = ?, dose_feel = ?,
                  side_effects = ?, notes = ?, updated_at = ?, updated_by = COALESCE(?, updated_by) WHERE id = ?""",
        (status, taken.strftime("%Y-%m-%d %H:%M") if taken else None, clean(values.get("dose")),
         clean(values.get("reason")), helped, feel, clean(effects), clean(values.get("notes")), utcnow(), by, log_id),
    )


def quick_follow_up(conn: sqlite3.Connection, log_id: int, helped: str | None, dose_feel: str | None,
                    by: int | None = None) -> None:
    sets, args = [], []
    if helped in HELPED:
        sets.append("helped = ?")
        args.append(helped)
    if dose_feel in DOSE_FEEL:
        sets.append("dose_feel = ?")
        args.append(dose_feel)
    if sets:
        conn.execute(
            f"UPDATE dose_logs SET {', '.join(sets)}, updated_at = ?, updated_by = COALESCE(?, updated_by) WHERE id = ?",
            [*args, utcnow(), by, log_id],
        )


def delete_dose_log(conn: sqlite3.Connection, log_id: int) -> None:
    conn.execute("DELETE FROM dose_logs WHERE id = ?", (log_id,))


def recent_doses(conn: sqlite3.Connection, med_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM dose_logs WHERE medication_id = ?
            ORDER BY COALESCE(taken_at, slot) DESC LIMIT ?""",
        (med_id, limit),
    ).fetchall()


def doses_between(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT dl.*, m.name AS med_name, m.brand AS med_brand, m.strength AS med_strength,
                  m.dose AS med_dose, m.route AS med_route, m.directions AS med_directions
             FROM dose_logs dl JOIN medications m ON m.id = dl.medication_id
            WHERE COALESCE(dl.taken_at, dl.slot) >= ? AND COALESCE(dl.taken_at, dl.slot) < ?
            ORDER BY COALESCE(dl.taken_at, dl.slot), m.name""",
        (start.isoformat(), (end + timedelta(days=1)).isoformat()),
    ).fetchall()


# ---------------------------------------------------------------- symptoms


def symptom_log(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM symptom_logs WHERE id = ?", (entry_id,)).fetchone()


def validate_symptom(values: dict, fallback: datetime, unit: str = "F") -> dict:
    key = values.get("symptom") or ""
    if key not in sx.BY_KEY:
        raise InvalidInput("Pick a symptom.")
    sym = sx.BY_KEY[key]
    severity: int | None = None
    value: float | None = None
    if sym.kind == "severity":
        severity = parse_int(_s(values.get("severity")), "How bad", 0, 3)
        if severity is None:
            raise InvalidInput(f"Choose how bad the {sym.name.lower()} is.")
    elif sym.kind == "temp":
        try:
            value, note = units.parse(_s(values.get("value")), unit)
        except units.BadTemperature as exc:
            raise InvalidInput(str(exc)) from None
        if note and note not in (values.get("notes") or ""):
            values = {**values, "notes": f"{note} {values.get('notes') or ''}".strip()}
    elif sym.kind == "pain":
        value = parse_float(_s(values.get("value")), "Pain", 0, 10)
        if value is None:
            raise InvalidInput("Choose a pain number from 0 to 10.")
    else:
        value = parse_int(_s(values.get("value")), "How many", 0, 99)
        if value is None:
            raise InvalidInput("Enter how many times.")
    label = clean(values.get("label"))
    if key == "other" and not label:
        raise InvalidInput("Say what the symptom is.")
    return {
        "symptom": key,
        "label": label,
        "severity": severity,
        "value": value,
        "notes": clean(values.get("notes")),
        "logged_at": parse_local_dt(values.get("logged_at"), fallback).strftime("%Y-%m-%d %H:%M"),
    }


def add_symptom(conn: sqlite3.Connection, values: dict, fallback: datetime, by: int | None = None) -> int:
    data = validate_symptom(values, fallback, get_settings(conn).get("temp_unit", "F"))
    now = utcnow()
    cur = conn.execute(
        """INSERT INTO symptom_logs (logged_at, symptom, label, severity, value, notes, logged_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (data["logged_at"], data["symptom"], data["label"], data["severity"], data["value"], data["notes"], by, now, now),
    )
    return rowid(cur)


def update_symptom(conn: sqlite3.Connection, entry_id: int, values: dict, fallback: datetime,
                   by: int | None = None) -> None:
    data = validate_symptom(values, fallback, get_settings(conn).get("temp_unit", "F"))
    conn.execute(
        """UPDATE symptom_logs SET logged_at = ?, symptom = ?, label = ?, severity = ?, value = ?, notes = ?,
                  updated_at = ?, updated_by = COALESCE(?, updated_by) WHERE id = ?""",
        (data["logged_at"], data["symptom"], data["label"], data["severity"], data["value"], data["notes"],
         utcnow(), by, entry_id),
    )


def delete_symptom(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM symptom_logs WHERE id = ?", (entry_id,))


def symptoms_on(conn: sqlite3.Connection, d: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM symptom_logs WHERE substr(logged_at, 1, 10) = ? ORDER BY logged_at DESC", (d.isoformat(),)
    ).fetchall()
