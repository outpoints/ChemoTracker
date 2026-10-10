"""Automatic backups.

A background thread wakes at the configured time (default 03:00 local, every
24 h) and writes a consistent SQLite snapshot plus the current Excel report
into BACKUP_DIR, keeping the newest BACKUP_KEEP snapshots. If the app was off
when a backup was due, it catches up shortly after starting.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import export
from .config import Settings
import re

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


class RestoreError(Exception):
    pass


@dataclass
class BackupFile:
    path: Path
    taken: datetime
    kind: str
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def describe(path: Path) -> BackupFile | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    suffix = m.group(3) or ""
    kind = next((v for k, v in KINDS.items() if k and suffix.startswith(k)), KINDS[""])
    return BackupFile(path, datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), kind, path.stat().st_size)


def backup_files(settings: Settings, limit: int = 40) -> list[BackupFile]:
    return [b for b in (describe(p) for p in list_backups(settings.backup_dir)[:limit]) if b]


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


def list_backups(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{PREFIX}*.sqlite3"), reverse=True)


def prune(folder: Path, keep: int) -> list[Path]:
    removed = []
    for old in list_backups(folder)[keep:]:
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
