from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, time

from .config import Settings
from .db import Database
from .emailer import ResendMailer, digest_email, notice_email
from .timeutil import SEOUL, iso_utc, now_utc


def next_digest_slot(now: datetime, hours: tuple[int, ...]) -> datetime:
    """Next weekday (Mon–Fri) digest time in Seoul; notices from the weekend go out at Monday's first slot."""
    local = now.astimezone(SEOUL)
    for days in range(8):
        day = local.date() + timedelta(days=days)
        if day.weekday() >= 5:
            continue
        for hour in hours:
            target = datetime.combine(day, time(hour, 0), SEOUL)
            if target > local:
                return target
    raise ValueError("DIGEST_HOURS must list at least one hour")


def _next_weekly(now: datetime, weekday: int, hour: int) -> datetime:
    local = now.astimezone(SEOUL)
    days = (weekday - local.weekday()) % 7
    target = datetime.combine(local.date() + timedelta(days=days), time(hour, 0), SEOUL)
    return target if target > local else target + timedelta(days=7)


class DeliveryPlanner:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def plan_notice(self, notice_id: int, analysis: dict) -> int:
        with self.db.connect() as conn:
            notice = conn.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            if not notice or notice["historical_import"]:
                if notice:
                    conn.execute("UPDATE notices SET processing_status='processed' WHERE id=?", (notice_id,))
                    conn.commit()
                return 0
            subscribers = conn.execute(
                """SELECT s.id,sub.immediate_enabled,sub.daily_digest_enabled,sub.weekly_digest_enabled
                   FROM subscribers s JOIN subscriptions sub ON sub.subscriber_id=s.id
                   WHERE s.status='active' AND sub.source_key=? AND sub.enabled=1""", (notice["source_key"],)
            ).fetchall()
            revision = conn.execute("SELECT 1 FROM notice_revisions WHERE notice_id=? AND new_hash=?",
                                    (notice_id, notice["content_hash"])).fetchone()
        now = now_utc()
        channels = {
            "immediate": ("updated_notice" if revision else "new_notice", "immediate_enabled", now),
            "daily": ("daily_digest", "daily_digest_enabled", next_digest_slot(now, self.settings.digest_hours)),
            "weekly": ("weekly_digest", "weekly_digest_enabled",
                       _next_weekly(now, self.settings.weekly_digest_weekday, self.settings.weekly_digest_hour)),
        }
        # Preferred channel first; fall back so a subscriber who disabled one frequency still gets the notice.
        # Normal and low notices never escalate to an immediate email.
        order = {"critical": ("immediate", "daily", "weekly"), "high": ("immediate", "daily", "weekly"),
                 "normal": ("daily", "weekly"), "low": ("weekly", "daily")}[analysis["importance"]]
        created = 0
        with self.db.transaction() as conn:
            # Same-content re-plans revive these through the upsert below; sent jobs are never touched.
            conn.execute("""UPDATE notification_deliveries SET status='cancelled',error='superseded by newer notice content'
                            WHERE notice_id=? AND status IN ('pending','retrying')""", (notice_id,))
            for subscriber in subscribers:
                channel = next((name for name in order if subscriber[channels[name][1]]), None)
                if channel is None:
                    continue
                kind, _, scheduled = channels[channel]
                dedupe = f"{kind}:{notice_id}:{notice['content_hash']}"
                cursor = conn.execute(
                    """INSERT INTO notification_deliveries(subscriber_id,notice_id,notification_type,
                       dedupe_key,scheduled_at,status,payload_json,created_at) VALUES(?,?,?,?,?,'pending',?,?)
                       ON CONFLICT(subscriber_id,dedupe_key) DO UPDATE SET status='pending',error=NULL,
                       scheduled_at=excluded.scheduled_at WHERE status='cancelled'""",
                    (subscriber["id"], notice_id, kind, dedupe, iso_utc(scheduled),
                     json.dumps({"content_hash": notice["content_hash"]}), iso_utc()),
                )
                created += cursor.rowcount
            conn.execute("UPDATE notices SET processing_status='processed' WHERE id=?", (notice_id,))
        created += self._plan_reminders(notice_id, notice["content_hash"], subscribers)
        return created

    def plan_pending(self, limit: int = 100) -> dict[str, int]:
        stats = {"completed": 0, "failed": 0, "created": 0}
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT n.id,n.source_key,a.result_json FROM notices n JOIN ai_analyses a
                   ON a.notice_id=n.id AND a.content_hash=n.content_hash
                   WHERE n.processing_status='delivery_pending' ORDER BY n.updated_at LIMIT ?""", (limit,)
            ).fetchall()
        for row in rows:
            try:
                stats["created"] += self.plan_notice(row["id"], json.loads(row["result_json"]))
                stats["completed"] += 1
            except Exception as exc:
                stats["failed"] += 1
                with self.db.transaction() as conn:
                    conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                                 ("delivery_planner", row["source_key"], str(exc)[:2000], iso_utc()))
        return stats

    def _plan_reminders(self, notice_id: int, content_hash: str, subscribers) -> int:
        now = now_utc()
        created = 0
        with self.db.transaction() as conn:
            deadlines = conn.execute("SELECT * FROM deadlines WHERE notice_id=? AND active=1", (notice_id,)).fetchall()
            for deadline in deadlines:
                deadline_at = datetime.fromisoformat(deadline["deadline_at"])
                for days, kind in ((7, "deadline_d7"), (3, "deadline_d3"), (1, "deadline_d1"), (0, "deadline_day")):
                    scheduled = deadline_at - timedelta(days=days)
                    if scheduled <= now:
                        continue
                    for subscriber in subscribers:
                        if not subscriber["immediate_enabled"]:
                            continue
                        dedupe = f"{kind}:{deadline['id']}:{content_hash}"
                        cursor = conn.execute(
                            """INSERT INTO notification_deliveries(subscriber_id,notice_id,notification_type,
                               dedupe_key,scheduled_at,status,payload_json,created_at) VALUES(?,?,?,?,?,'pending',?,?)
                       ON CONFLICT(subscriber_id,dedupe_key) DO UPDATE SET status='pending',error=NULL,
                       scheduled_at=excluded.scheduled_at WHERE status='cancelled'""",
                            (subscriber["id"], notice_id, kind, dedupe, iso_utc(scheduled),
                             json.dumps({"deadline_id": deadline["id"]}), iso_utc()),
                        )
                        created += cursor.rowcount
        return created


class DeliveryWorker:
    def __init__(self, db: Database, settings: Settings, mailer: ResendMailer):
        self.db = db
        self.settings = settings
        self.mailer = mailer

    def _eligible(self, conn, delivery) -> tuple[dict, dict, dict] | None:
        row = conn.execute(
            """SELECT s.*,sub.enabled,sub.immediate_enabled,sub.daily_digest_enabled,sub.weekly_digest_enabled,
               n.original_title,n.original_url,n.published_at,n.source_key,src.name AS source_name,a.result_json
               FROM notification_deliveries d JOIN subscribers s ON s.id=d.subscriber_id
               JOIN notices n ON n.id=d.notice_id JOIN sources src ON src.source_key=n.source_key
               JOIN subscriptions sub ON sub.subscriber_id=s.id AND sub.source_key=n.source_key
               JOIN ai_analyses a ON a.notice_id=n.id AND a.content_hash=n.content_hash WHERE d.id=?""",
            (delivery["id"],),
        ).fetchone()
        if not row or row["status"] != "active" or not row["enabled"]:
            return None
        kind = delivery["notification_type"]
        if kind in ("new_notice", "updated_notice") or kind.startswith("deadline_"):
            enabled = row["immediate_enabled"]
        elif kind == "daily_digest":
            enabled = row["daily_digest_enabled"]
        else:
            enabled = row["weekly_digest_enabled"]
        if not enabled:
            return None
        notice = {key: row[key] for key in ("original_title", "original_url", "published_at", "source_key", "source_name")}
        return dict(row), notice, json.loads(row["result_json"])

    def process_due(self, limit: int = 100) -> dict[str, int]:
        now = iso_utc()
        stale_cutoff = iso_utc(now_utc() - timedelta(minutes=15))
        with self.db.transaction() as conn:
            conn.execute("""UPDATE notification_deliveries SET status='retrying',next_attempt_at=?,
                            error='recovered after interrupted send worker'
                            WHERE status='sending' AND scheduled_at<?
                            AND notification_type NOT IN ('verification','welcome','subscription_management')""",
                         (now, stale_cutoff))
        with self.db.connect() as conn:
            due = conn.execute(
                """SELECT * FROM notification_deliveries WHERE status IN ('pending','retrying')
                   AND scheduled_at<=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   AND notification_type NOT IN ('verification','welcome','subscription_management')
                   ORDER BY scheduled_at LIMIT ?""", (now, now, limit),
            ).fetchall()
        digest_groups: dict[tuple[int, str], list] = defaultdict(list)
        singles = []
        for item in due:
            if item["notification_type"] in ("daily_digest", "weekly_digest"):
                digest_groups[(item["subscriber_id"], item["notification_type"])].append(item)
            else:
                singles.append([item])
        batches = singles + list(digest_groups.values())
        stats = {"sent": 0, "failed": 0, "skipped": 0}
        for batch in batches:
            ids = [item["id"] for item in batch]
            with self.db.transaction() as conn:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"UPDATE notification_deliveries SET status='sending' WHERE id IN ({placeholders})", ids)
            try:
                entries = []
                subscriber = None
                with self.db.connect() as conn:
                    for item in batch:
                        eligible = self._eligible(conn, item)
                        if eligible:
                            subscriber, notice, analysis = eligible
                            entries.append((notice, analysis))
                if not entries or subscriber is None:
                    with self.db.transaction() as conn:
                        placeholders = ",".join("?" for _ in ids)
                        conn.execute(f"UPDATE notification_deliveries SET status='cancelled',error='subscriber no longer eligible' WHERE id IN ({placeholders})", ids)
                    stats["skipped"] += len(ids)
                    continue
                kind = batch[0]["notification_type"]
                if kind in ("daily_digest", "weekly_digest"):
                    subject, body = digest_email(self.settings, subscriber, entries, kind == "weekly_digest")
                else:
                    subject, body = notice_email(self.settings, subscriber, entries[0][0], entries[0][1], kind)
                    if kind.startswith("deadline_"):
                        subject = f"[截止提醒] {subject.removeprefix('[重要公告] ')}"
                    if json.loads(batch[0]["payload_json"] or "{}").get("test"):
                        subject = f"[测试] {subject}"
                key = "delivery-" + "-".join(str(value) for value in ids)
                provider_id = self.mailer.send(subscriber["email"], subject, body, key[:250])
                with self.db.transaction() as conn:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(f"UPDATE notification_deliveries SET status='sent',sent_at=?,provider_message_id=? WHERE id IN ({placeholders})",
                                 [iso_utc(), provider_id, *ids])
                stats["sent"] += len(ids)
            except Exception as exc:
                for item in batch:
                    attempts = item["attempts"] + 1
                    terminal = attempts >= self.settings.max_delivery_attempts
                    retry_at = iso_utc(now_utc() + timedelta(minutes=min(2 ** attempts, 60)))
                    with self.db.transaction() as conn:
                        conn.execute("""UPDATE notification_deliveries SET status=?,attempts=?,next_attempt_at=?,error=? WHERE id=?""",
                                     ("failed" if terminal else "retrying", attempts, None if terminal else retry_at,
                                      str(exc)[:2000], item["id"]))
                stats["failed"] += len(ids)
        return stats
