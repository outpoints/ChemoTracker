"""Runtime configuration from environment variables.

Everything personal (database, backups) lives outside the repo: DATA_DIR and
BACKUP_DIR are git-ignored and mounted as Docker volumes.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PACKAGE_DIR = Path(__file__).resolve().parent


def _timezone() -> ZoneInfo:
    name = os.environ.get("CHEMO_TIMEZONE") or os.environ.get("TZ")
    if not name:
        # macOS / Debian expose the system zone through the /etc/localtime symlink.
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _secret_key(data_dir: Path) -> str:
    env = os.environ.get("CHEMO_SECRET_KEY")
    if env:
        return env
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    return key


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    tz: ZoneInfo
    password: str | None
    secret_key: str
    backup_dir: Path
    backup_interval_hours: float
    backup_time: time
    backup_keep: int
    backup_enabled: bool


def load_settings(data_dir: str | Path | None = None, backup_dir: str | Path | None = None) -> Settings:
    root = Path(data_dir or os.environ.get("CHEMO_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    backups = Path(backup_dir or os.environ.get("CHEMO_BACKUP_DIR") or root / "backups").resolve()
    try:
        backup_time = time.fromisoformat(os.environ.get("CHEMO_BACKUP_TIME", "03:00"))
    except ValueError:
        backup_time = time(3, 0)
    return Settings(
        data_dir=root,
        db_path=root / "chemotracker.sqlite3",
        tz=_timezone(),
        password=os.environ.get("CHEMO_PASSWORD") or None,
        secret_key=_secret_key(root),
        backup_dir=backups,
        backup_interval_hours=max(_float("CHEMO_BACKUP_INTERVAL_HOURS", 24), 0.25),
        backup_time=backup_time,
        backup_keep=max(int(_float("CHEMO_BACKUP_KEEP", 30)), 1),
        backup_enabled=os.environ.get("CHEMO_BACKUP_ENABLED", "1").lower() not in ("0", "false", "no", "off"),
    )
