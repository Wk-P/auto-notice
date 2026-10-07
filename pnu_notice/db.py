from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
  source_key TEXT PRIMARY KEY, name TEXT NOT NULL, page_url TEXT NOT NULL,
  rss_url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  backfill_completed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL REFERENCES sources(source_key),
  external_notice_id TEXT NOT NULL, original_title TEXT NOT NULL,
  original_url TEXT NOT NULL, author TEXT, published_at TEXT,
  category_original TEXT, raw_html TEXT NOT NULL, clean_text TEXT NOT NULL,
  external_links_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
  historical_import INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
  ai_status TEXT NOT NULL DEFAULT 'pending', processing_status TEXT NOT NULL,
  ai_attempts INTEGER NOT NULL DEFAULT 0, ai_next_attempt_at TEXT,
  UNIQUE(source_key, external_notice_id)
);
CREATE INDEX IF NOT EXISTS idx_notices_source_published ON notices(source_key, published_at);
CREATE INDEX IF NOT EXISTS idx_notices_ai_status ON notices(ai_status);
CREATE TABLE IF NOT EXISTS notice_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL REFERENCES notices(id),
  old_hash TEXT NOT NULL, new_hash TEXT NOT NULL, detected_at TEXT NOT NULL,
  old_raw_html TEXT NOT NULL, UNIQUE(notice_id, new_hash)
);
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, url TEXT NOT NULL, extension TEXT NOT NULL,
  extracted_text TEXT, parse_status TEXT NOT NULL DEFAULT 'pending',
  UNIQUE(notice_id, url)
);
CREATE TABLE IF NOT EXISTS ai_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  content_hash TEXT NOT NULL, result_json TEXT NOT NULL, model TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(notice_id, content_hash)
);
CREATE TABLE IF NOT EXISTS deadlines (
  id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, deadline_at TEXT NOT NULL, timezone TEXT NOT NULL,
  original_text TEXT NOT NULL, confidence REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(notice_id, kind, deadline_at)
);
CREATE TABLE IF NOT EXISTS subscribers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, email_normalized TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('pending','active','paused','unsubscribed','email_invalid')),
  created_at TEXT NOT NULL, verified_at TEXT, unsubscribed_at TEXT, language TEXT NOT NULL DEFAULT 'zh-CN',
  verification_token_hash TEXT, verification_expires_at TEXT,
  management_token_hash TEXT, management_token_created_at TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  source_key TEXT NOT NULL REFERENCES sources(source_key), enabled INTEGER NOT NULL DEFAULT 1,
  immediate_enabled INTEGER NOT NULL DEFAULT 1, daily_digest_enabled INTEGER NOT NULL DEFAULT 1,
  weekly_digest_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(subscriber_id, source_key)
);
CREATE TABLE IF NOT EXISTS email_verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL,
  preferences_json TEXT NOT NULL DEFAULT '{}', poll_token_hash TEXT
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_id INTEGER NOT NULL REFERENCES subscribers(id),
  notice_id INTEGER REFERENCES notices(id), channel TEXT NOT NULL DEFAULT 'email',
  notification_type TEXT NOT NULL, dedupe_key TEXT NOT NULL,
  scheduled_at TEXT NOT NULL, sent_at TEXT, status TEXT NOT NULL,
  provider_message_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  UNIQUE(subscriber_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_due ON notification_deliveries(status, scheduled_at, next_attempt_at);
CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL REFERENCES sources(source_key),
  run_type TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  items_seen INTEGER NOT NULL DEFAULT 0, new_items INTEGER NOT NULL DEFAULT 0,
  updated_items INTEGER NOT NULL DEFAULT 0, error_message TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, email_normalized TEXT NOT NULL UNIQUE,
  subscriber_id INTEGER UNIQUE REFERENCES subscribers(id), role TEXT NOT NULL CHECK(role IN ('user','admin')),
  password_hash TEXT, must_change_password INTEGER NOT NULL DEFAULT 0, password_changed_at TEXT,
  failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, last_login_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email_normalized TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, component TEXT NOT NULL, source_key TEXT,
  error_message TEXT NOT NULL, occurred_at TEXT NOT NULL, alerted_at TEXT
);
"""

# Columns added after the first release; CREATE TABLE IF NOT EXISTS does not add them to existing databases.
MIGRATIONS = (
    ("notices", "ai_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("notices", "ai_next_attempt_at", "TEXT"),
    ("email_verifications", "preferences_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("email_verifications", "poll_token_hash", "TEXT"),
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def backup(self, destination: Path | str) -> None:
        target_path = Path(destination)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
