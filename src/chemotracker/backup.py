"""Automatic backups.

A background thread wakes at the configured time (default 03:00 local, every
24 h) and writes a consistent SQLite snapshot plus the current Excel report
into BACKUP_DIR, keeping the newest BACKUP_KEEP snapshots. If the app was off
when a backup was due, it catches up shortly after starting.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import export
from .config import Settings
from .db import connect, utcnow

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


def run_backup(settings: Settings, reason: str = "scheduled") -> BackupResult:
    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    target = settings.backup_dir / f"{PREFIX}{stamp}.sqlite3"
    try:
        settings.backup_dir.mkdir(parents=True, exist_ok=True)
        src = connect(settings.db_path)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
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
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:  # pragma: no cover - never let bookkeeping break a backup
        log.exception("could not record backup")


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
