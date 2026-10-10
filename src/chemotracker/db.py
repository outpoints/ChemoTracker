"""SQLite storage. One file, versioned with PRAGMA user_version.

Times people see (dose slots, taken_at, symptom times) are local wall-clock
strings 'YYYY-MM-DD HH:MM' in the configured timezone, because that is what
the care team reads. Audit columns (created_at/updated_at) are UTC ISO-8601.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- What the pharmacy label says. One row per medicine.
    CREATE TABLE medications (
        id                INTEGER PRIMARY KEY,
        name              TEXT NOT NULL,
        brand             TEXT,
        info_key          TEXT,
        category          TEXT,
        route             TEXT NOT NULL DEFAULT 'oral',
        strength          TEXT,
        dose              TEXT,
        directions        TEXT,
        min_hours_between REAL CHECK (min_hours_between IS NULL OR min_hours_between > 0),
        max_per_day       INTEGER CHECK (max_per_day IS NULL OR max_per_day > 0),
        notes             TEXT,
        active            INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );

    -- When a medicine is used, relative to each cycle's Day 1 (Day 0 = day before chemo).
    -- end_day NULL = through the end of the cycle. to_cycle NULL = every cycle from from_cycle on.
    CREATE TABLE plans (
        id            INTEGER PRIMARY KEY,
        medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('scheduled', 'as_needed', 'clinic')),
        start_day     INTEGER NOT NULL CHECK (start_day >= 0),
        end_day       INTEGER CHECK (end_day IS NULL OR end_day >= start_day),
        times         TEXT NOT NULL DEFAULT '',
        label         TEXT,
        from_cycle    INTEGER NOT NULL DEFAULT 1 CHECK (from_cycle >= 1),
        to_cycle      INTEGER CHECK (to_cycle IS NULL OR to_cycle >= from_cycle),
        created_at    TEXT NOT NULL
    );
    CREATE INDEX plans_med ON plans(medication_id);

    CREATE TABLE cycles (
        id          INTEGER PRIMARY KEY,
        number      INTEGER NOT NULL UNIQUE CHECK (number > 0),
        day1        TEXT NOT NULL,
        length_days INTEGER NOT NULL DEFAULT 14 CHECK (length_days BETWEEN 1 AND 90),
        regimen     TEXT,
        notes       TEXT,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE dose_logs (
        id            INTEGER PRIMARY KEY,
        medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
        plan_id       INTEGER REFERENCES plans(id) ON DELETE SET NULL,
        kind          TEXT NOT NULL DEFAULT 'as_needed',
        slot          TEXT,
        status        TEXT NOT NULL CHECK (status IN ('taken', 'skipped')),
        taken_at      TEXT,
        dose          TEXT,
        reason        TEXT,
        helped        TEXT CHECK (helped IN ('yes', 'some', 'no', 'unsure')),
        dose_feel     TEXT CHECK (dose_feel IN ('right', 'too_strong', 'not_enough')),
        side_effects  TEXT,
        notes         TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE UNIQUE INDEX dose_logs_plan_slot ON dose_logs(plan_id, slot)
        WHERE plan_id IS NOT NULL AND slot IS NOT NULL;
    CREATE INDEX dose_logs_med_time ON dose_logs(medication_id, taken_at);

    -- Symptoms, logged as often as needed. severity 0-3 (none/mild/moderate/severe);
    -- value holds a number for temperature (F), counts (times), or pain (0-10).
    CREATE TABLE symptom_logs (
        id         INTEGER PRIMARY KEY,
        logged_at  TEXT NOT NULL,
        symptom    TEXT NOT NULL,
        label      TEXT,
        severity   INTEGER CHECK (severity BETWEEN 0 AND 3),
        value      REAL,
        notes      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX symptom_logs_time ON symptom_logs(logged_at);

    CREATE TABLE backups (
        id         INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        reason     TEXT NOT NULL,
        path       TEXT,
        bytes      INTEGER,
        ok         INTEGER NOT NULL,
        error      TEXT
    );
    """,
    # v2: people who use the app, and who logged / last edited each entry.
    # Profiles are attribution, not security: anyone on a device can switch.
    """
    CREATE TABLE profiles (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT 'family' CHECK (role IN ('patient', 'family', 'caregiver')),
        color      TEXT NOT NULL,
        active     INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    ALTER TABLE dose_logs ADD COLUMN logged_by INTEGER REFERENCES profiles(id) ON DELETE SET NULL;
    ALTER TABLE dose_logs ADD COLUMN updated_by INTEGER REFERENCES profiles(id) ON DELETE SET NULL;
    ALTER TABLE symptom_logs ADD COLUMN logged_by INTEGER REFERENCES profiles(id) ON DELETE SET NULL;
    ALTER TABLE symptom_logs ADD COLUMN updated_by INTEGER REFERENCES profiles(id) ON DELETE SET NULL;
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(path: Path | str) -> int:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {index};\nCOMMIT;")
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@contextmanager
def session(path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rowid(cur: sqlite3.Cursor) -> int:
    if cur.lastrowid is None:  # pragma: no cover - sqlite always sets it after INSERT
        raise RuntimeError("insert did not return a row id")
    return cur.lastrowid


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS: dict[str, str] = {
    "patient_name": "",
    "clinic_name": "",
    "clinic_phone": "",
    "after_hours_phone": "",
    "fever_f": "100.4",
    "call_if": (
        "Temperature at or above your fever line, or shaking chills\n"
        "Can't keep liquids or medicines down\n"
        "Anything listed under \"Call your care team\" on a medicine's page"
    ),
    "onboarded": "",
}


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    values.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return values


def put_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(k, v) for k, v in values.items() if k in DEFAULT_SETTINGS],
    )
