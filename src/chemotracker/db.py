"""SQLite storage. One file, versioned with PRAGMA user_version.

Times people see (dose slots, taken_at, symptom times) are local wall-clock
strings 'YYYY-MM-DD HH:MM' in the configured timezone, because that is what
the care team reads. Audit columns (created_at/updated_at) are UTC ISO-8601.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

log = logging.getLogger("chemotracker.db")

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
    # v3: a dose per schedule, for tapers like "4 mg the evening of chemo, then 8 mg twice daily".
    """
    ALTER TABLE plans ADD COLUMN dose TEXT;
    """,
    # v4: change history (every create, change and delete, with who and the before/after values),
    # and the reminders already sent so a restart never repeats one.
    """
    CREATE TABLE history (
        id     INTEGER PRIMARY KEY,
        at     TEXT NOT NULL,
        actor  INTEGER,
        entity TEXT NOT NULL,
        ref    TEXT,
        action TEXT NOT NULL CHECK (action IN ('created', 'changed', 'deleted', 'restored')),
        before TEXT,
        after  TEXT
    );
    CREATE INDEX history_entity ON history(entity, ref, id);
    CREATE INDEX history_at ON history(at);

    CREATE TABLE reminders_sent (
        key     TEXT PRIMARY KEY,
        sent_at TEXT NOT NULL
    );

    -- A step before each dose, e.g. "Take it out of the fridge" 30 minutes before a shot.
    ALTER TABLE medications ADD COLUMN prep_minutes INTEGER CHECK (prep_minutes IS NULL OR prep_minutes BETWEEN 1 AND 1440);
    ALTER TABLE medications ADD COLUMN prep_note TEXT;
    -- White-cell booster syringes: the FDA labels say to let them warm up for 30 minutes.
    UPDATE medications SET prep_minutes = 30, prep_note = 'Take it out of the fridge'
     WHERE info_key IN ('filgrastim', 'pegfilgrastim') AND route = 'injection' AND prep_minutes IS NULL;
    """,
    # v5: an id is never handed out twice. Without AUTOINCREMENT SQLite gives a new row the id of the newest
    # deleted one, so an Undo button, a follow-up answer or a history line holding that id could act on, or
    # show, a different entry. Each table is rebuilt with the same columns in the same order, every row is
    # copied as it is (checked before the old table goes), and the counters start above every id the table
    # has ever used, deleted ones included. migrate() runs this with foreign keys off: dropping the old
    # medications table with them on would delete every dose and schedule that points at it.
    """
    CREATE TEMP TABLE upgrade_check (ok INTEGER NOT NULL CHECK (ok = 1));

    CREATE TABLE medications_v5 (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
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
        updated_at        TEXT NOT NULL,
        prep_minutes      INTEGER CHECK (prep_minutes IS NULL OR prep_minutes BETWEEN 1 AND 1440),
        prep_note         TEXT
    );
    INSERT INTO medications_v5 (id, name, brand, info_key, category, route, strength, dose, directions,
                                min_hours_between, max_per_day, notes, active, created_at, updated_at,
                                prep_minutes, prep_note)
         SELECT id, name, brand, info_key, category, route, strength, dose, directions,
                min_hours_between, max_per_day, notes, active, created_at, updated_at, prep_minutes, prep_note
           FROM medications;
    INSERT INTO upgrade_check
         SELECT NOT EXISTS (SELECT * FROM medications EXCEPT SELECT * FROM medications_v5)
            AND NOT EXISTS (SELECT * FROM medications_v5 EXCEPT SELECT * FROM medications);
    DROP TABLE medications;
    ALTER TABLE medications_v5 RENAME TO medications;

    CREATE TABLE plans_v5 (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('scheduled', 'as_needed', 'clinic')),
        start_day     INTEGER NOT NULL CHECK (start_day >= 0),
        end_day       INTEGER CHECK (end_day IS NULL OR end_day >= start_day),
        times         TEXT NOT NULL DEFAULT '',
        label         TEXT,
        from_cycle    INTEGER NOT NULL DEFAULT 1 CHECK (from_cycle >= 1),
        to_cycle      INTEGER CHECK (to_cycle IS NULL OR to_cycle >= from_cycle),
        created_at    TEXT NOT NULL,
        dose          TEXT
    );
    INSERT INTO plans_v5 (id, medication_id, kind, start_day, end_day, times, label, from_cycle, to_cycle,
                          created_at, dose)
         SELECT id, medication_id, kind, start_day, end_day, times, label, from_cycle, to_cycle, created_at, dose
           FROM plans;
    INSERT INTO upgrade_check
         SELECT NOT EXISTS (SELECT * FROM plans EXCEPT SELECT * FROM plans_v5)
            AND NOT EXISTS (SELECT * FROM plans_v5 EXCEPT SELECT * FROM plans);
    DROP TABLE plans;
    ALTER TABLE plans_v5 RENAME TO plans;
    CREATE INDEX plans_med ON plans(medication_id);

    CREATE TABLE cycles_v5 (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        number      INTEGER NOT NULL UNIQUE CHECK (number > 0),
        day1        TEXT NOT NULL,
        length_days INTEGER NOT NULL DEFAULT 14 CHECK (length_days BETWEEN 1 AND 90),
        regimen     TEXT,
        notes       TEXT,
        created_at  TEXT NOT NULL
    );
    INSERT INTO cycles_v5 (id, number, day1, length_days, regimen, notes, created_at)
         SELECT id, number, day1, length_days, regimen, notes, created_at FROM cycles;
    INSERT INTO upgrade_check
         SELECT NOT EXISTS (SELECT * FROM cycles EXCEPT SELECT * FROM cycles_v5)
            AND NOT EXISTS (SELECT * FROM cycles_v5 EXCEPT SELECT * FROM cycles);
    DROP TABLE cycles;
    ALTER TABLE cycles_v5 RENAME TO cycles;

    CREATE TABLE dose_logs_v5 (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
        updated_at    TEXT NOT NULL,
        logged_by     INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
        updated_by    INTEGER REFERENCES profiles(id) ON DELETE SET NULL
    );
    INSERT INTO dose_logs_v5 (id, medication_id, plan_id, kind, slot, status, taken_at, dose, reason, helped,
                              dose_feel, side_effects, notes, created_at, updated_at, logged_by, updated_by)
         SELECT id, medication_id, plan_id, kind, slot, status, taken_at, dose, reason, helped,
                dose_feel, side_effects, notes, created_at, updated_at, logged_by, updated_by
           FROM dose_logs;
    INSERT INTO upgrade_check
         SELECT NOT EXISTS (SELECT * FROM dose_logs EXCEPT SELECT * FROM dose_logs_v5)
            AND NOT EXISTS (SELECT * FROM dose_logs_v5 EXCEPT SELECT * FROM dose_logs);
    DROP TABLE dose_logs;
    ALTER TABLE dose_logs_v5 RENAME TO dose_logs;
    CREATE UNIQUE INDEX dose_logs_plan_slot ON dose_logs(plan_id, slot)
        WHERE plan_id IS NOT NULL AND slot IS NOT NULL;
    CREATE INDEX dose_logs_med_time ON dose_logs(medication_id, taken_at);

    CREATE TABLE symptom_logs_v5 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_at  TEXT NOT NULL,
        symptom    TEXT NOT NULL,
        label      TEXT,
        severity   INTEGER CHECK (severity BETWEEN 0 AND 3),
        value      REAL,
        notes      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        logged_by  INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
        updated_by INTEGER REFERENCES profiles(id) ON DELETE SET NULL
    );
    INSERT INTO symptom_logs_v5 (id, logged_at, symptom, label, severity, value, notes, created_at, updated_at,
                                 logged_by, updated_by)
         SELECT id, logged_at, symptom, label, severity, value, notes, created_at, updated_at, logged_by, updated_by
           FROM symptom_logs;
    INSERT INTO upgrade_check
         SELECT NOT EXISTS (SELECT * FROM symptom_logs EXCEPT SELECT * FROM symptom_logs_v5)
            AND NOT EXISTS (SELECT * FROM symptom_logs_v5 EXCEPT SELECT * FROM symptom_logs);
    DROP TABLE symptom_logs;
    ALTER TABLE symptom_logs_v5 RENAME TO symptom_logs;
    CREATE INDEX symptom_logs_time ON symptom_logs(logged_at);

    -- Start each counter above every id ever used, including deleted entries that history still points at.
    DELETE FROM sqlite_sequence WHERE name IN ('medications', 'plans', 'cycles', 'dose_logs', 'symptom_logs',
        'medications_v5', 'plans_v5', 'cycles_v5', 'dose_logs_v5', 'symptom_logs_v5');
    INSERT INTO sqlite_sequence (name, seq) VALUES
        ('medications', MAX((SELECT IFNULL(MAX(id), 0) FROM medications),
            (SELECT IFNULL(MAX(CAST(ref AS INTEGER)), 0) FROM history WHERE entity = 'medication'))),
        ('plans', MAX((SELECT IFNULL(MAX(id), 0) FROM plans),
            (SELECT IFNULL(MAX(CAST(ref AS INTEGER)), 0) FROM history WHERE entity = 'plan'))),
        ('cycles', MAX((SELECT IFNULL(MAX(id), 0) FROM cycles),
            (SELECT IFNULL(MAX(CAST(ref AS INTEGER)), 0) FROM history WHERE entity = 'cycle'))),
        ('dose_logs', MAX((SELECT IFNULL(MAX(id), 0) FROM dose_logs),
            (SELECT IFNULL(MAX(CAST(ref AS INTEGER)), 0) FROM history WHERE entity = 'dose'))),
        ('symptom_logs', MAX((SELECT IFNULL(MAX(id), 0) FROM symptom_logs),
            (SELECT IFNULL(MAX(CAST(ref AS INTEGER)), 0) FROM history WHERE entity = 'symptom')));
    INSERT INTO upgrade_check SELECT NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check) OR
        (SELECT COUNT(*) FROM pragma_foreign_key_check) <= (SELECT n FROM temp.fk_before);
    DROP TABLE temp.upgrade_check;
    DROP TABLE temp.fk_before;
    """,
    # v6: Archive. A medicine with logged doses can't be deleted (the care team's report needs its doses), so it
    # can be put away instead. An archived medicine is also stopped (active = 0). Added at the end of the table, so
    # older code that reads columns by name still opens the database.
    """
    ALTER TABLE medications ADD COLUMN archived_at TEXT;
    """,
]

# ---------------------------------------------------------------- change history
#
# Every write connection gets TEMP triggers that copy each insert, update and delete on
# these tables into `history`, with the person using the device. Triggers catch
# everything, including cascades (removing a medicine removes its doses), so no code
# path can change the record without leaving a trace. They are TEMP so the database
# file stays plain SQLite that any tool can open.

AUDITED: dict[str, tuple[str, str]] = {
    # table: (entity name, key column)
    "dose_logs": ("dose", "id"),
    "symptom_logs": ("symptom", "id"),
    "medications": ("medication", "id"),
    "plans": ("plan", "id"),
    "cycles": ("cycle", "id"),
    "profiles": ("person", "id"),
    "settings": ("setting", "key"),
}
# Bookkeeping columns: kept in the snapshot, but a change to only these isn't a change.
NOT_CONTENT = {"created_at", "updated_at", "updated_by", "logged_by"}
# Never copied into history.
SECRET_SETTINGS = ("ntfy_token",)


def _json(conn: sqlite3.Connection, table: str, side: str, content_only: bool = False) -> str:
    cols = [r["name"] for r in conn.execute(f"PRAGMA main.table_info({table})")]
    parts = []
    for c in cols:
        if content_only and c in NOT_CONTENT:
            continue
        value = f"{side}.{c}"
        if table == "settings" and c == "value":
            secret = ", ".join(f"'{k}'" for k in SECRET_SETTINGS)
            value = f"CASE WHEN {side}.key IN ({secret}) THEN '(hidden)' ELSE {side}.value END"
        parts.append(f"'{c}', {value}")
    return f"json_object({', '.join(parts)})"


def _install_audit(conn: sqlite3.Connection, actor: int | None) -> None:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS audit_ctx (actor INTEGER)")
    conn.execute("DELETE FROM temp.audit_ctx")
    conn.execute("INSERT INTO temp.audit_ctx (actor) VALUES (?)", (actor,))
    stamp = "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')"
    who = "(SELECT actor FROM audit_ctx LIMIT 1)"
    for table, (entity, key) in AUDITED.items():
        new, old = _json(conn, table, "NEW"), _json(conn, table, "OLD")
        new_c, old_c = _json(conn, table, "NEW", True), _json(conn, table, "OLD", True)
        conn.execute(f"""CREATE TEMP TRIGGER IF NOT EXISTS audit_{table}_ins AFTER INSERT ON main.{table} BEGIN
            INSERT INTO history (at, actor, entity, ref, action, after)
            VALUES ({stamp}, {who}, '{entity}', NEW.{key}, 'created', {new}); END""")
        conn.execute(f"""CREATE TEMP TRIGGER IF NOT EXISTS audit_{table}_upd AFTER UPDATE ON main.{table}
            WHEN {old_c} IS NOT {new_c} BEGIN
            INSERT INTO history (at, actor, entity, ref, action, before, after)
            VALUES ({stamp}, {who}, '{entity}', NEW.{key}, 'changed', {old}, {new}); END""")
        conn.execute(f"""CREATE TEMP TRIGGER IF NOT EXISTS audit_{table}_del AFTER DELETE ON main.{table} BEGIN
            INSERT INTO history (at, actor, entity, ref, action, before)
            VALUES ({stamp}, {who}, '{entity}', OLD.{key}, 'deleted', {old}); END""")


def record_event(conn: sqlite3.Connection, entity: str, ref: str | None, action: str, actor: int | None,
                 before: str | None = None, after: str | None = None) -> None:
    """A history line for something triggers can't see (a whole database restored from a backup)."""
    conn.execute(
        "INSERT INTO history (at, actor, entity, ref, action, before, after) VALUES (?,?,?,?,?,?,?)",
        (utcnow(), actor, entity, ref, action, before, after),
    )


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    # isolation_level=None: transactions are started and ended explicitly (see `session`),
    # so a read-then-write can't be split by another writer.
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    # WAL + FULL: a power cut can't lose a dose someone was told was saved.
    conn.execute("PRAGMA synchronous = FULL")
    return conn


class DataDirInUse(RuntimeError):
    pass


def lock_data_dir(data_dir: Path):
    """Refuse to run two servers on one database (two containers, or `docker compose` plus a dev server).

    Returns the open lock file; keep a reference for as long as the app runs.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: no advisory locks, single instance is on you
        return None
    handle = open(data_dir / ".chemotracker.lock", "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise DataDirInUse(
            f"Another ChemoTracker is already using {data_dir}. Stop it first: two servers on one database can lose entries."
        ) from None
    return handle


def schema_version(path: Path | str) -> int:
    conn = connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def migrate(path: Path | str) -> int:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= len(MIGRATIONS):
            return version
        # Foreign keys stay off while the structure changes: rebuilding a table drops the old one, and with
        # them on that would also delete every row pointing at it. Each step checks its own work instead
        # (v5 compares every copied row and the broken-link count) and is all-or-nothing.
        conn.execute("PRAGMA foreign_keys = OFF")
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                "CREATE TEMP TABLE IF NOT EXISTS fk_before AS SELECT COUNT(*) AS n FROM pragma_foreign_key_check;\n"
                f"{script}\nDROP TABLE IF EXISTS temp.fk_before;\nPRAGMA user_version = {index};\nCOMMIT;"
            )
        conn.execute("PRAGMA foreign_keys = ON")
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def quick_check(path: Path | str) -> str:
    """'ok', or SQLite's description of the first problem it finds."""
    conn = connect(path)
    try:
        problems = [r[0] for r in conn.execute("PRAGMA quick_check(5)")]
        if problems == ["ok"]:
            bad_links = conn.execute("PRAGMA foreign_key_check").fetchall()
            return f"{len(bad_links)} entries point at rows that no longer exist" if bad_links else "ok"
        return "; ".join(problems)
    finally:
        conn.close()


@contextmanager
def session(path: Path | str, write: bool = False, actor: int | None = None) -> Iterator[sqlite3.Connection]:
    """One transaction per request.

    Reads get a consistent snapshot (BEGIN). Writes take the write lock up front
    (BEGIN IMMEDIATE), so "check, then save" can't interleave with another person's
    save, and a busy database waits instead of failing.
    """
    conn = connect(path)
    try:
        if write and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'history'").fetchone():
            _install_audit(conn, actor)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing for a group of writes inside a session (e.g. a series of cycles)."""
    conn.execute("SAVEPOINT grp")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK TO grp")
        conn.execute("RELEASE grp")
        raise
    conn.execute("RELEASE grp")


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
    "temp_unit": "F",
    "call_if": (
        "Temperature at or above your fever line, or shaking chills\n"
        "Can't keep liquids or medicines down\n"
        "Anything listed under \"Call your care team\" on a medicine's page"
    ),
    "onboarded": "",
    # Reminders through ntfy (see reminders.py).
    "ntfy_enabled": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic_draft": "",
    "ntfy_topic": "",
    "ntfy_token": "",
    "ntfy_lead": "0",
    "ntfy_nag": "30",
    "ntfy_chemo": "1",
    "ntfy_names": "1",
    # "Tell me when it's OK again" for as-needed medicines: ticked medicine ids, the morning reminder, and when
    # the overnight hold starts (it ends at the morning reminder). "off" turns either off.
    "ntfy_ok_meds": "",
    "ntfy_morning": "09:00",
    "ntfy_quiet_from": "02:00",
    "app_url": "",
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
