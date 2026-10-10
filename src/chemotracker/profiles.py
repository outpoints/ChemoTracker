"""People who use the app: the patient, family, and caregivers.

Each device remembers who is using it (a signed session cookie). Every dose
and symptom entry records who logged it and who last edited it, so the care
team can see "logged by Jane" in the report. This is attribution, not access
control: the family password (if set) is the only lock.
"""

from __future__ import annotations

import sqlite3

from .db import rowid, utcnow

ROLES = {"patient": "Patient", "family": "Family", "caregiver": "Caregiver"}
# Distinct, legible on white text in light and night mode.
COLORS = ("#11616a", "#a24a16", "#3d6b33", "#8a3658", "#3b42a0", "#7a5a00", "#4a6a8a", "#6a4a9a")


class InvalidProfile(ValueError):
    pass


def initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper() if len(parts[0]) > 1 else parts[0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def all_profiles(conn: sqlite3.Connection, active: bool | None = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM profiles"
    if active is not None:
        sql += f" WHERE active = {1 if active else 0}"
    return conn.execute(sql + " ORDER BY CASE role WHEN 'patient' THEN 0 ELSE 1 END, lower(name)").fetchall()


def get(conn: sqlite3.Connection, profile_id: int | None) -> sqlite3.Row | None:
    if not profile_id:
        return None
    return conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()


def by_id(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {p["id"]: p for p in conn.execute("SELECT * FROM profiles")}


def _clean(name: str | None, role: str | None) -> tuple[str, str]:
    name = " ".join((name or "").split())
    if not name:
        raise InvalidProfile("Enter a name.")
    if len(name) > 40:
        raise InvalidProfile("Keep the name under 40 characters.")
    return name, role if role in ROLES else "family"


def create(conn: sqlite3.Connection, name: str | None, role: str | None = "family") -> int:
    clean_name, clean_role = _clean(name, role)
    existing = conn.execute(
        "SELECT id FROM profiles WHERE lower(name) = lower(?) AND active = 1", (clean_name,)
    ).fetchone()
    if existing:
        raise InvalidProfile(f"There's already someone called {clean_name}.")
    count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO profiles (name, role, color, created_at) VALUES (?,?,?,?)",
        (clean_name, clean_role, COLORS[count % len(COLORS)], utcnow()),
    )
    return rowid(cur)


def update(conn: sqlite3.Connection, profile_id: int, name: str | None, role: str | None) -> None:
    clean_name, clean_role = _clean(name, role)
    clash = conn.execute(
        "SELECT id FROM profiles WHERE lower(name) = lower(?) AND active = 1 AND id != ?", (clean_name, profile_id)
    ).fetchone()
    if clash:
        raise InvalidProfile(f"There's already someone called {clean_name}.")
    conn.execute("UPDATE profiles SET name = ?, role = ? WHERE id = ?", (clean_name, clean_role, profile_id))


def set_active(conn: sqlite3.Connection, profile_id: int, active: bool) -> None:
    conn.execute("UPDATE profiles SET active = ? WHERE id = ?", (1 if active else 0, profile_id))


def entry_counts(conn: sqlite3.Connection) -> dict[int, tuple[int, int]]:
    """profile id -> (doses logged, symptoms logged)."""
    doses = dict(conn.execute("SELECT logged_by, COUNT(*) FROM dose_logs WHERE logged_by IS NOT NULL GROUP BY logged_by"))
    syms = dict(conn.execute("SELECT logged_by, COUNT(*) FROM symptom_logs WHERE logged_by IS NOT NULL GROUP BY logged_by"))
    return {pid: (doses.get(pid, 0), syms.get(pid, 0)) for pid in set(doses) | set(syms)}


def name_of(people: dict[int, sqlite3.Row], profile_id: int | None) -> str:
    p = people.get(profile_id) if profile_id else None
    return p["name"] if p else ""


def attribution(people: dict[int, sqlite3.Row], logged_by: int | None, updated_by: int | None) -> str:
    who = name_of(people, logged_by)
    editor = name_of(people, updated_by)
    if editor and editor != who:
        return f"{who} (edited by {editor})" if who else f"edited by {editor}"
    return who
