from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, time

from .config import Settings
from .db import Database
from .emailer import ResendMailer, digest_email, notice_email, reminder_email
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


REMINDER_DAYS = (7, 3, 1, 0)
# Notices published before someone subscribed (including the historical backfill) only get the last two reminders,
# so a new subscriber is not flooded with D-7 and D-3 mails for everything already announced.
LATE_REMINDER_DAYS = (1, 0)
REMINDER_KINDS = {7: "deadline_d7", 3: "deadline_d3", 1: "deadline_d1", 0: "deadline_day"}
# Reminders whose time passed while the scheduler was down are skipped rather than sent in a burst.
REMINDER_GRACE = timedelta(hours=2)
REMINDER_MIN_CONFIDENCE = 0.6
STUDENT_ACTION_WORDS = ("截止", "截至", "期限", "deadline", "申请", "报名", "提交", "缴费", "缴纳", "注册", "登记", "办理")
NOT_STUDENT_ACTION_WORDS = ("非学生", "教职", "学院向", "开始")


def is_student_deadline(kind: str, confidence: float) -> bool:
    """Only deadlines a student has to act on are reminded; event times, exam times and staff-side dates are not.
    Uses the type label the AI already produced, so it costs no extra AI calls."""
    label = (kind or "").casefold()
    return (confidence >= REMINDER_MIN_CONFIDENCE and any(word in label for word in STUDENT_ACTION_WORDS)
            and not any(word in label for word in NOT_STUDENT_ACTION_WORDS))


def end_of_day_if_date_only(deadline_at: datetime) -> datetime:
    """Notices often give only a date ("10/8까지"), which arrives as 00:00; that means until the end of that day."""
    local = deadline_at.astimezone(SEOUL)
    return local.replace(hour=23, minute=59) if (local.hour, local.minute, local.second) == (0, 0, 0) else local


def reminder_time(deadline_at: datetime, days: int, hours: tuple[int, ...]) -> datetime:
    """Reminders go out with the first digest slot (10:00 Seoul) on the day N days before the deadline. A same-day
    reminder that would arrive less than 2 hours before the deadline moves to the previous evening's last slot."""
    local = end_of_day_if_date_only(deadline_at)
    day = local.date() - timedelta(days=days)
    at = datetime.combine(day, time(min(hours), 0), SEOUL)
    if days == 0 and at > local - timedelta(hours=2):
        at = datetime.combine(day - timedelta(days=1), time(max(hours), 0), SEOUL)
    return at


class ReminderPlanner:
    """Creates deadline reminders when they fall due, for whoever is subscribed at that moment, so people who
    subscribe later are included and people who left are not."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def run(self, now: datetime | None = None) -> int:
        now = now or now_utc()
        created = 0
        with self.db.transaction() as conn:
            # Reminders used to be scheduled per subscriber when a notice was analysed; those are replaced by this.
            conn.execute("""UPDATE notification_deliveries SET status='cancelled',error='replaced by scheduled reminders'
                            WHERE notification_type LIKE 'deadline_%' AND status IN ('pending','retrying')
                            AND dedupe_key NOT LIKE 'deadline:%'""")
            deadlines = conn.execute(
                """SELECT d.*,n.created_at AS notice_created,n.source_key FROM deadlines d JOIN notices n ON n.id=d.notice_id
                   WHERE d.active=1 AND d.deadline_at>?""", (iso_utc(now),)).fetchall()
            for deadline in deadlines:
                if not is_student_deadline(deadline["kind"], deadline["confidence"]):
                    continue
                deadline_at = datetime.fromisoformat(deadline["deadline_at"])
                for days in REMINDER_DAYS:
                    at = reminder_time(deadline_at, days, self.settings.digest_hours)
                    if not now - REMINDER_GRACE < at <= now:
                        continue
                    subscribers = conn.execute(
                        """SELECT s.id,s.verified_at FROM subscribers s JOIN subscriptions sub ON sub.subscriber_id=s.id
                           WHERE s.status='active' AND sub.source_key=? AND sub.enabled=1 AND sub.immediate_enabled=1""",
                        (deadline["source_key"],)).fetchall()
                    for subscriber in subscribers:
                        published_before_subscribing = (subscriber["verified_at"] or "") >= deadline["notice_created"]
                        if published_before_subscribing and days not in LATE_REMINDER_DAYS:
                            continue
                        cursor = conn.execute(
                            """INSERT OR IGNORE INTO notification_deliveries(subscriber_id,notice_id,notification_type,
                               dedupe_key,scheduled_at,status,payload_json,created_at) VALUES(?,?,?,?,?,'pending',?,?)""",
                            (subscriber["id"], deadline["notice_id"], REMINDER_KINDS[days],
                             f"deadline:{deadline['id']}:{days}", iso_utc(now),
                             json.dumps({"deadline_id": deadline["id"], "days": days}), iso_utc(now)))
                        created += cursor.rowcount
        return created


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
            elif item["notification_type"].startswith("deadline_"):
                # All reminders due for one person in this round become a single email.
                digest_groups[(item["subscriber_id"], "deadline")].append(item)
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
                included = []
                subscriber = None
                with self.db.connect() as conn:
                    for item in batch:
                        eligible = self._eligible(conn, item)
                        if not eligible:
                            continue
                        if item["notification_type"].startswith("deadline_"):
                            deadline = conn.execute("SELECT * FROM deadlines WHERE id=? AND active=1 AND deadline_at>?",
                                                    (json.loads(item["payload_json"])["deadline_id"], now)).fetchone()
                            if not deadline:  # deadline withdrawn by a notice update, or already passed
                                continue
                            eligible = (*eligible, dict(deadline))
                        subscriber, notice, analysis = eligible[:3]
                        entries.append((notice, analysis, *eligible[3:]))
                        included.append(item["id"])
                if not entries or subscriber is None:
                    with self.db.transaction() as conn:
                        placeholders = ",".join("?" for _ in ids)
                        conn.execute(f"UPDATE notification_deliveries SET status='cancelled',error='subscriber no longer eligible' WHERE id IN ({placeholders})", ids)
                    stats["skipped"] += len(ids)
                    continue
                kind = batch[0]["notification_type"]
                if kind in ("daily_digest", "weekly_digest"):
                    subject, body = digest_email(self.settings, subscriber, [entry[:2] for entry in entries],
                                                 kind == "weekly_digest")
                elif kind.startswith("deadline_"):
                    subject, body = reminder_email(self.settings, subscriber, entries)
                else:
                    subject, body = notice_email(self.settings, subscriber, entries[0][0], entries[0][1], kind)
                    if json.loads(batch[0]["payload_json"] or "{}").get("test"):
                        subject = f"[测试] {subject}"
                key = "delivery-" + "-".join(str(value) for value in ids)
                provider_id = self.mailer.send(subscriber["email"], subject, body, key[:250])
                skipped = [item_id for item_id in ids if item_id not in included]
                with self.db.transaction() as conn:
                    conn.execute(f"UPDATE notification_deliveries SET status='sent',sent_at=?,provider_message_id=? "
                                 f"WHERE id IN ({','.join('?' for _ in included)})", [iso_utc(), provider_id, *included])
                    if skipped:
                        conn.execute(f"UPDATE notification_deliveries SET status='cancelled',error='subscriber no longer eligible' "
                                     f"WHERE id IN ({','.join('?' for _ in skipped)})", skipped)
                stats["sent"] += len(included)
                stats["skipped"] += len(skipped)
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
