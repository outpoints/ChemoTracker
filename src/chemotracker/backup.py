"""Automatic backups.

A background thread wakes at the configured time (default 03:00 local, every
24 h) and writes a consistent SQLite snapshot plus the current Excel report
into BACKUP_DIR, keeping the newest BACKUP_KEEP snapshots. If the app was off
when a backup was due, it catches up shortly after starting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import export
from .config import Settings
from .db import MIGRATIONS, connect, migrate, quick_check, record_event, session, utcnow

log = logging.getLogger("chemotracker.backup")
PREFIX = "chemotracker-"


@dataclass
class BackupResult:
    ok: bool
    path: Path | None
    bytes: int
    error: str | None = None


def local_now(settings: Settings) -> datetime:
    return datetime.now(settings.tz).replace(tzinfo=None, second=0, microsecond=0)


def free_name(folder: Path, stem: str) -> Path:
    """`stem.sqlite3`, or `stem-2.sqlite3` and so on if two copies land in the same second."""
    target, n = folder / f"{stem}.sqlite3", 1
    while target.exists():
        n += 1
        target = folder / f"{stem}-{n}.sqlite3"
    return target


def snapshot(settings: Settings, target: Path) -> None:
    """Copy the live database to `target` safely while people keep using the app.

    Uses SQLite's online backup (a consistent copy even mid-write), writes to a
    temporary name, checks the copy, and only then gives it its real name, so a
    half-written or damaged file never looks like a good backup.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    src = connect(settings.db_path)
    try:
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    try:
        result = quick_check(partial)
        if result != "ok":
            raise RuntimeError(f"the copy failed its check ({result})")
        with open(partial, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def run_backup(settings: Settings, reason: str = "scheduled") -> BackupResult:
    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    target = free_name(settings.backup_dir, f"{PREFIX}{stamp}")
    try:
        snapshot(settings, target)
        src = connect(settings.db_path)
        try:
            src.execute("BEGIN")
            report = _report(src, settings)
        finally:
            src.close()
        target.with_suffix(".xlsx").write_bytes(report)
        size = target.stat().st_size
        _record(settings, reason, target, size, True, None)
        prune(settings.backup_dir, settings.backup_keep)
        log.info("backup written: %s (%d bytes)", target, size)
        return BackupResult(True, target, size)
    except Exception as exc:  # noqa: BLE001 - recorded and shown in Settings
        log.exception("backup failed")
        _record(settings, reason, target, 0, False, f"{type(exc).__name__}: {exc}")
        return BackupResult(False, None, 0, str(exc))


def _report(conn: sqlite3.Connection, settings: Settings) -> bytes:
    now = local_now(settings)
    first = conn.execute(
        "SELECT MIN(d) FROM (SELECT MIN(day1) AS d FROM cycles UNION SELECT MIN(substr(logged_at,1,10)) FROM symptom_logs"
        " UNION SELECT MIN(substr(COALESCE(taken_at, slot),1,10)) FROM dose_logs)"
    ).fetchone()[0]
    start = date.fromisoformat(first) - timedelta(days=1) if first else now.date()
    return export.build_workbook(conn, start, now.date(), now)


def _record(settings: Settings, reason: str, path: Path, size: int, ok: bool, error: str | None) -> None:
    try:
        conn = connect(settings.db_path)
        try:
            conn.execute(
                "INSERT INTO backups (created_at, reason, path, bytes, ok, error) VALUES (?,?,?,?,?,?)",
                (utcnow(), reason, str(path), size, 1 if ok else 0, error),
            )
            conn.execute("DELETE FROM backups WHERE id NOT IN (SELECT id FROM backups ORDER BY id DESC LIMIT 200)")
        finally:
            conn.close()
    except sqlite3.Error:  # pragma: no cover - never let bookkeeping break a backup
        log.exception("could not record backup")


# ---------------------------------------------------------------- restore

NAME_RE = re.compile(r"^chemotracker-(\d{8})-(\d{6})(-[a-z0-9-]+)?\.sqlite3$")
KINDS = {"": "Backup", "-before-restore": "Saved before a restore", "-uploaded": "Uploaded",
         "-before-upgrade": "Saved before an update"}
# Copies that are the way back from a restore or an update, and files someone uploaded. The regular rotation
# (BACKUP_KEEP) never removes them, or the "switch back" copy would be gone a few backups after a restore.
# Each kind keeps its newest KEEP_SPECIAL.
SPECIAL = ("-before-restore", "-before-upgrade", "-uploaded")
KEEP_SPECIAL = 10


class RestoreError(Exception):
    pass


@dataclass
class BackupFile:
    path: Path
    taken: datetime  # what the backup holds: the record as it was at this time
    kind: str
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def describe(path: Path, tz: ZoneInfo | None = None) -> BackupFile | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    suffix = m.group(3) or ""
    kind = next((v for k, v in KINDS.items() if k and suffix.startswith(k)), KINDS[""])
    taken = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if suffix.startswith("-uploaded"):
        # Its name has the upload time. What matters is when it was made: everything after that goes.
        taken = made_at(path, tz) or taken
    return BackupFile(path, taken, kind, path.stat().st_size)


def made_at(path: Path, tz: ZoneInfo | None = None) -> datetime | None:
    """Local time of the newest change inside a backup file (history, entries, medicines, earlier backups)."""
    sources = {"history": ("at",), "dose_logs": ("created_at", "updated_at"), "symptom_logs": ("created_at", "updated_at"),
               "medications": ("created_at", "updated_at"), "plans": ("created_at",), "cycles": ("created_at",),
               "profiles": ("created_at",), "backups": ("created_at",)}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        newest = [conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
                  for table, cols in sources.items() if table in tables for col in cols]
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    stamps = []
    for value in newest:
        try:
            stamps.append(datetime.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    stamps = [t for t in stamps if t.tzinfo is not None]  # audit times are UTC; anything else isn't one
    if not stamps:
        return None
    return max(stamps).astimezone(tz).replace(tzinfo=None, microsecond=0)


def backup_files(settings: Settings, limit: int = 40) -> list[BackupFile]:
    return [b for b in (describe(p, settings.tz) for p in list_backups(settings.backup_dir)[:limit]) if b]


def find(settings: Settings, name: str) -> Path:
    """A backup in the backup folder, by file name only (no paths from the browser)."""
    if not NAME_RE.match(name or ""):
        raise RestoreError("That isn't a ChemoTracker backup file.")
    path = settings.backup_dir / name
    if not path.is_file():
        raise RestoreError("That backup isn't in the backup folder any more.")
    return path


def inspect(path: Path) -> dict:
    """What's inside a backup, without changing it."""
    with open(path, "rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            raise RestoreError("That file isn't a ChemoTracker backup.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"medications", "dose_logs", "symptom_logs", "cycles", "settings"} <= tables:
            raise RestoreError("That file isn't a ChemoTracker backup.")
        check = [r[0] for r in conn.execute("PRAGMA quick_check(3)")]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
        last = conn.execute(
            "SELECT MAX(t) FROM (SELECT MAX(taken_at) AS t FROM dose_logs UNION SELECT MAX(logged_at) FROM symptom_logs)"
        ).fetchone()[0]
        return {"ok": check == ["ok"], "problem": "; ".join(check), "version": version,
                "doses": count("dose_logs"), "symptoms": count("symptom_logs"),
                "medicines": count("medications"), "cycles": count("cycles"), "last": last}
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"That backup can't be read ({exc}).") from None
    finally:
        conn.close()


@dataclass
class Changes:
    """What restoring a backup does to the record as it is now, entry by entry."""
    lost: list[str] = field(default_factory=list)       # logged since the backup: gone after the restore
    set_back: list[str] = field(default_factory=list)   # changed since: back to how they were
    returning: list[str] = field(default_factory=list)  # deleted since: back in the record
    other: list[str] = field(default_factory=list)      # medicines, schedules, cycles, people, settings

    @property
    def empty(self) -> bool:
        return not (self.lost or self.set_back or self.returning or self.other)


def _rows(conn: sqlite3.Connection, table: str) -> dict[tuple, dict]:
    """Rows by (id, created_at): an id alone isn't enough, because before v5 ids could be reused."""
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.DatabaseError:
        return {}
    out = {}
    for r in rows:
        d = dict(zip(r.keys(), tuple(r)))
        out[(d.get("id", d.get("key")), d.get("created_at"))] = d
    return out


def _lookup(conn: sqlite3.Connection, sql: str) -> dict:
    try:
        return {r[0]: r[1] for r in conn.execute(sql)}
    except sqlite3.DatabaseError:
        return {}


def _entry_text(kind: str, row: dict, meds: dict, people: dict) -> str:
    from . import schedule
    from . import symptoms as sx

    who = people.get(row.get("updated_by") or row.get("logged_by"))
    by = f", logged by {who}" if who else ""
    if kind == "dose":
        name = meds.get(row.get("medication_id")) or "A medicine"
        when = schedule.parse_local(row.get("taken_at"))
        if row.get("status") == "skipped" or when is None:
            slot = row.get("slot") or ""
            planned = schedule.parse_local(slot) if len(slot) > 10 else None
            day = planned.date() if planned else (date.fromisoformat(slot[:10]) if slot[:10] else None)
            at = f" at {schedule.fmt_time(planned)}" if planned else ""
            plan_text = f" (planned {schedule.fmt_day(day)}{at})" if day else ""
            verb = "skipped" if row.get("status") == "skipped" else "given"
            return f"{name}, {verb}{plan_text}{by}"
        verb = "given" if row.get("kind") == "clinic" else "taken"
        return f"{name}, {verb} {schedule.fmt_day(when.date())} at {schedule.fmt_time(when)}{by}"
    s = sx.lookup(row.get("symptom") or "")
    name = row.get("label") or (s.name if s else "A symptom")
    when = schedule.parse_local(row.get("logged_at"))
    at = f", {schedule.fmt_day(when.date())} at {schedule.fmt_time(when)}" if when else ""
    return f"{name}{at}{by}"


def _differs(a: dict, b: dict) -> bool:
    """Compared on the columns both have, so an older backup's missing columns don't count as changes."""
    return any(a[k] != b[k] for k in set(a) & set(b))


def _names_list(names: list[str]) -> str:
    names = sorted(dict.fromkeys(n for n in names if n))
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else (names[0] if names else "")


def compare(conn: sqlite3.Connection, path: Path) -> Changes:
    """Compare the live record (`conn`) with a backup file: what a restore would remove, set back or bring back."""
    from . import history

    out = Changes()
    bk = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    bk.row_factory = sqlite3.Row
    try:
        now_meds = _lookup(conn, "SELECT id, name FROM medications")
        old_meds = _lookup(bk, "SELECT id, name FROM medications")
        now_people = _lookup(conn, "SELECT id, name FROM profiles")
        old_people = _lookup(bk, "SELECT id, name FROM profiles")
        for kind, table in (("dose", "dose_logs"), ("symptom", "symptom_logs")):
            now_rows, old_rows = _rows(conn, table), _rows(bk, table)
            order = (lambda r: (r.get("taken_at") or r.get("slot") or "")) if kind == "dose" else (lambda r: r.get("logged_at") or "")
            for key in sorted(set(now_rows) - set(old_rows), key=lambda k: order(now_rows[k])):
                out.lost.append(_entry_text(kind, now_rows[key], now_meds, now_people))
            for key in sorted(set(now_rows) & set(old_rows), key=lambda k: order(now_rows[k])):
                if now_rows[key].get("updated_at") != old_rows[key].get("updated_at"):
                    out.set_back.append(_entry_text(kind, old_rows[key], old_meds, old_people))
            for key in sorted(set(old_rows) - set(now_rows), key=lambda k: order(old_rows[k])):
                out.returning.append(_entry_text(kind, old_rows[key], old_meds, old_people))
        now_m, old_m = _rows(conn, "medications"), _rows(bk, "medications")
        added = [now_m[k]["name"] for k in set(now_m) - set(old_m)]
        gone = [old_m[k]["name"] for k in set(old_m) - set(now_m)]
        edited = [now_m[k]["name"] for k in set(now_m) & set(old_m) if _differs(now_m[k], old_m[k])]
        if added:
            out.other.append(f"Medicines added since then are removed: {_names_list(added)}.")
        if gone:
            out.other.append(f"Medicines deleted since then come back: {_names_list(gone)}.")
        if edited:
            out.other.append(f"Changes made since then to {_names_list(edited)} are undone.")
        now_p, old_p = _rows(conn, "plans"), _rows(bk, "plans")
        plans = [now_meds.get(now_p[k]["medication_id"]) for k in set(now_p) - set(old_p)]
        plans += [old_meds.get(old_p[k]["medication_id"]) for k in set(old_p) - set(now_p)]
        plans += [now_meds.get(now_p[k]["medication_id"]) for k in set(now_p) & set(old_p) if _differs(now_p[k], old_p[k])]
        if plans:
            out.other.append(f"Schedules go back to how they were for {_names_list([p or 'a medicine' for p in plans])}.")
        now_c, old_c = _rows(conn, "cycles"), _rows(bk, "cycles")
        cycles = [f"Cycle {now_c[k]['number']}" for k in set(now_c) - set(old_c)]
        cycles += [f"Cycle {old_c[k]['number']}" for k in set(old_c) - set(now_c)]
        cycles += [f"Cycle {now_c[k]['number']}" for k in set(now_c) & set(old_c) if _differs(now_c[k], old_c[k])]
        if cycles:
            out.other.append(f"Cycle dates go back to how they were for {_names_list(cycles)}.")
        people = [name for pid, name in now_people.items() if pid not in old_people]
        if people:
            out.other.append(f"People added since then are removed: {_names_list(people)}. Their entries since "
                             "then are listed above.")
        now_s = _lookup(conn, "SELECT key, value FROM settings")
        old_s = _lookup(bk, "SELECT key, value FROM settings")
        settings = [history.SETTING_NAMES[k] for k in history.SETTING_NAMES if now_s.get(k) != old_s.get(k)
                    and k != "onboarded"]
        if settings:
            out.other.append(f"Settings go back to how they were: {_names_list(settings)}.")
    finally:
        bk.close()
    return out


def restore(settings: Settings, path: Path, actor: int | None) -> Path:
    """Replace the live record with `path`. Saves the current record first and returns that copy."""
    info = inspect(path)
    if not info["ok"]:
        raise RestoreError(f"That backup is damaged ({info['problem']}). Pick an earlier one.")
    if info["version"] > len(MIGRATIONS):
        raise RestoreError("That backup was made by a newer version of ChemoTracker. Update the app first.")
    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    safety = free_name(settings.backup_dir, f"{PREFIX}{stamp}-before-restore")
    snapshot(settings, safety)
    # Reminders that already went out stay sent: the backup's list is older, and a phone must never be told
    # again that a dose is due (perhaps one given since).
    live = connect(settings.db_path)
    try:
        sent = live.execute("SELECT key, sent_at FROM reminders_sent").fetchall()
    except sqlite3.OperationalError:
        sent = []
    finally:
        live.close()
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dst = connect(settings.db_path)
        try:
            src.backup(dst)  # SQLite's online copy: other requests wait, then see the restored record
        finally:
            dst.close()
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"The restore didn't finish ({exc}). Nothing was changed.") from None
    finally:
        src.close()
    migrate(settings.db_path)
    with session(settings.db_path, write=True, actor=actor) as conn:
        conn.executemany("INSERT OR IGNORE INTO reminders_sent (key, sent_at) VALUES (?, ?)", [tuple(r) for r in sent])
        record_event(conn, "database", path.name, "restored", actor)
    log.info("restored %s (current record saved as %s)", path.name, safety.name)
    return safety


def save_upload(settings: Settings, chunks, limit: int = 512 * 1024 * 1024) -> Path:
    """Store an uploaded backup in the backup folder so it can be checked and restored like any other."""
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    target = free_name(settings.backup_dir, f"{PREFIX}{stamp}-uploaded")
    partial = target.with_name(target.name + ".partial")
    size = 0
    try:
        with open(partial, "wb") as fh:
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise RestoreError("That file is too big to be a ChemoTracker backup.")
                fh.write(chunk)
        inspect(partial)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def _newest_first(path: Path) -> tuple:
    """Time in the name, then the -2, -3 a second copy in the same second gets (it is the newer one)."""
    m = NAME_RE.match(path.name)
    if not m:
        return ("", 0, path.name)
    n = re.search(r"-(\d+)$", m.group(3) or "")
    return (m.group(1) + m.group(2), int(n.group(1)) if n else 1, path.name)


def list_backups(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{PREFIX}*.sqlite3"), key=_newest_first, reverse=True)


def special_kind(path: Path) -> str | None:
    m = NAME_RE.match(path.name)
    suffix = (m.group(3) or "") if m else ""
    return next((k for k in SPECIAL if suffix.startswith(k)), None)


def prune(folder: Path, keep: int) -> list[Path]:
    """Keep the newest `keep` regular backups, and the newest KEEP_SPECIAL of each special kind."""
    removed = []
    regular: list[Path] = []
    special: dict[str, list[Path]] = {}
    for p in list_backups(folder):
        kind = special_kind(p)
        (special.setdefault(kind, []) if kind else regular).append(p)
    for old in regular[keep:] + [p for files in special.values() for p in files[KEEP_SPECIAL:]]:
        for f in (old, old.with_suffix(".xlsx")):
            if f.exists():
                f.unlink()
                removed.append(f)
    return removed


def last_success(settings: Settings) -> datetime | None:
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT created_at FROM backups WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return datetime.fromisoformat(row["created_at"]).astimezone(settings.tz).replace(tzinfo=None)


def next_run(settings: Settings, after: datetime) -> datetime:
    """Next run on the grid anchored at backup_time and spaced by the interval."""
    step = timedelta(hours=settings.backup_interval_hours)
    anchor = datetime.combine(after.date(), settings.backup_time)
    while anchor > after:
        anchor -= step
    while anchor <= after:
        anchor += step
    return anchor


def is_overdue(settings: Settings, now: datetime) -> bool:
    last = last_success(settings)
    return last is None or now - last >= timedelta(hours=settings.backup_interval_hours)


class BackupScheduler:
    def __init__(self, settings: Settings, catch_up_delay: float = 60.0) -> None:
        self.settings = settings
        self.catch_up_delay = catch_up_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.next_at: datetime | None = None

    def start(self) -> None:
        if not self.settings.backup_enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="backup-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        now = local_now(self.settings)
        self.next_at = next_run(self.settings, now)
        if is_overdue(self.settings, now) and schedule_has_data(self.settings):
            if self._stop.wait(self.catch_up_delay):
                return
            run_backup(self.settings, "catch-up")
        while not self._stop.is_set():
            now = local_now(self.settings)
            self.next_at = next_run(self.settings, now)
            wait = (self.next_at - datetime.now(self.settings.tz).replace(tzinfo=None)).total_seconds()
            if self._stop.wait(max(wait, 1)):
                return
            run_backup(self.settings, "scheduled")


def schedule_has_data(settings: Settings) -> bool:
    conn = connect(settings.db_path)
    try:
        return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM medications)").fetchone()[0])
    finally:
        conn.close()
