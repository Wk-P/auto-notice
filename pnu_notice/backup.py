from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Database
from .timeutil import now_utc


def create_backup(db: Database, directory: Path, retention_days: int = 14) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    now = now_utc()
    target = directory / f"pnu-notice-{now.strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    db.backup(target)
    cutoff = now - timedelta(days=retention_days)
    for candidate in directory.glob("pnu-notice-*.sqlite3"):
        try:
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff and candidate != target:
                candidate.unlink()
        except (OSError, ValueError):
            continue
    return target
