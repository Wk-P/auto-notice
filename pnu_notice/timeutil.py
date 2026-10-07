from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or now_utc()
    if value.tzinfo is None:
        value = value.replace(tzinfo=SEOUL)
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = " ".join(value.strip().split())
    try:
        result = datetime.fromisoformat(raw)
        return result.replace(tzinfo=SEOUL) if result.tzinfo is None else result
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M %z"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    normalized = raw.replace("/", "-")
    normalized = re.sub(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", r"\1-\2-\3", normalized)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            result = datetime.strptime(normalized, fmt)
            return result.replace(tzinfo=SEOUL) if result.tzinfo is None else result
        except ValueError:
            pass
    return None
