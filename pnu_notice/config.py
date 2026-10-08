from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_path: Path
    app_base_url: str
    app_secret: str
    poll_interval_minutes: int
    backfill_start: str
    requests_per_second: float
    http_timeout_seconds: int
    openai_api_key: str | None
    openai_model: str
    resend_api_key: str | None
    from_name: str
    from_email: str
    admin_email: str | None
    digest_hours: tuple[int, ...]
    weekly_digest_weekday: int
    weekly_digest_hour: int
    token_ttl_hours: int
    verification_cooldown_seconds: int
    max_delivery_attempts: int
    turnstile_site_key: str | None = None
    turnstile_secret: str | None = None
    turnstile_hostnames: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "var/pnu_notice.sqlite3")),
            app_base_url=os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
            app_secret=os.getenv("APP_SECRET", "development-only-change-me"),
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "60")),
            backfill_start=os.getenv("BACKFILL_START", "2026-07-01T00:00:00+09:00"),
            requests_per_second=float(os.getenv("REQUESTS_PER_SECOND", "1")),
            http_timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
            resend_api_key=os.getenv("RESEND_API_KEY") or None,
            from_name=os.getenv("FROM_NAME", "PNU Notice"),
            from_email=os.getenv("FROM_EMAIL", "notice@example.com"),
            admin_email=os.getenv("ADMIN_EMAIL") or None,
            digest_hours=tuple(sorted(int(hour) for hour in os.getenv("DIGEST_HOURS", "10,13,16,19").split(",")
                                      if hour.strip())),
            weekly_digest_weekday=int(os.getenv("WEEKLY_DIGEST_WEEKDAY", "4")),
            weekly_digest_hour=int(os.getenv("WEEKLY_DIGEST_HOUR", "19")),
            token_ttl_hours=int(os.getenv("TOKEN_TTL_HOURS", "24")),
            verification_cooldown_seconds=int(os.getenv("VERIFICATION_COOLDOWN_SECONDS", "300")),
            max_delivery_attempts=int(os.getenv("MAX_DELIVERY_ATTEMPTS", "5")),
            turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY") or None,
            turnstile_secret=os.getenv("TURNSTILE_SECRET") or None,
            turnstile_hostnames=tuple(host.strip() for host in os.getenv("TURNSTILE_HOSTNAMES", "").split(",")
                                      if host.strip()),
        )
