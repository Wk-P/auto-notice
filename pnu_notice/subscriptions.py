from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta

from .config import Settings
from .db import Database
from .emailer import ResendMailer, welcome_email
from .security import management_token, random_token, token_hash
from .sources import SOURCE_BY_KEY
from .timeutil import iso_utc, now_utc

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SubscriptionService:
    def __init__(self, db: Database, settings: Settings, mailer: ResendMailer):
        self.db = db
        self.settings = settings
        self.mailer = mailer

    def subscribe(self, email: str, source_keys: list[str], immediate: bool, daily: bool, weekly: bool) -> str:
        """Sends the confirmation email and returns a poll token that lets the submitting page see when it is confirmed."""
        normalized = email.strip().casefold()
        selected = list(dict.fromkeys(source_keys))
        if not EMAIL_RE.fullmatch(normalized) or len(normalized) > 254:
            raise ValueError("请输入有效的邮箱地址。")
        if not selected or any(key not in SOURCE_BY_KEY for key in selected):
            raise ValueError("请至少选择一个公告来源。")
        if not any((immediate, daily, weekly)):
            raise ValueError("请至少选择一种通知频率。")
        token = random_token()
        digest = token_hash(token, self.settings.app_secret)
        poll_token = random_token()
        now = iso_utc()
        expires = iso_utc(now_utc() + timedelta(hours=self.settings.token_ttl_hours))
        with self.db.transaction() as conn:
            subscriber = conn.execute("SELECT * FROM subscribers WHERE email_normalized=?", (normalized,)).fetchone()
            if subscriber:
                subscriber_id = subscriber["id"]
                # Only a verification email that actually went out starts the cooldown.
                latest = conn.execute(
                    """SELECT created_at FROM notification_deliveries WHERE subscriber_id=?
                       AND notification_type='verification' AND status='sent' ORDER BY id DESC LIMIT 1""",
                    (subscriber_id,),
                ).fetchone()
                if latest:
                    age = (now_utc() - datetime.fromisoformat(latest["created_at"])).total_seconds()
                    if age < self.settings.verification_cooldown_seconds:
                        raise ValueError("验证邮件刚刚已经发送，请稍后再试。")
                if subscriber["status"] in ("active", "paused"):
                    # Anyone can type this address, so a working subscription is left untouched until the
                    # mailbox owner confirms the new preferences.
                    conn.execute("UPDATE subscribers SET verification_token_hash=?,verification_expires_at=? WHERE id=?",
                                 (digest, expires, subscriber_id))
                else:
                    conn.execute("""UPDATE subscribers SET email=?,status='pending',verification_token_hash=?,
                                    verification_expires_at=?,verified_at=NULL WHERE id=?""",
                                 (email.strip(), digest, expires, subscriber_id))
            else:
                subscriber_id = conn.execute(
                    """INSERT INTO subscribers(email,email_normalized,status,created_at,verification_token_hash,
                       verification_expires_at) VALUES(?,?,'pending',?,?,?)""",
                    (email.strip(), normalized, now, digest, expires),
                ).lastrowid
            preferences = {"sources": selected, "immediate": immediate, "daily": daily, "weekly": weekly}
            conn.execute("UPDATE email_verifications SET used_at=? WHERE subscriber_id=? AND used_at IS NULL", (now, subscriber_id))
            conn.execute("""INSERT INTO email_verifications(subscriber_id,token_hash,expires_at,created_at,preferences_json,
                            poll_token_hash) VALUES(?,?,?,?,?,?)""",
                         (subscriber_id, digest, expires, now, json.dumps(preferences),
                          token_hash(poll_token, self.settings.app_secret)))
            delivery_id = conn.execute(
                """INSERT INTO notification_deliveries(subscriber_id,notification_type,dedupe_key,scheduled_at,status,created_at)
                   VALUES(?,'verification',?,?,'sending',?)""", (subscriber_id, f"verification:{digest}", now, now),
            ).lastrowid
        verify_url = f"{self.settings.app_base_url}/verify?token={token}"
        body = ("<main style='font-family:Arial,sans-serif;max-width:560px;margin:auto;line-height:1.7;color:#17213b'>"
                "<p style='color:#174b9b;font-weight:bold'>PNU Notice</p>"
                "<h1 style='font-size:24px'>还差一步：确认你的订阅</h1>"
                "<p>点击下面的按钮后订阅立即生效。<strong>在确认之前，你不会收到任何公告邮件。</strong></p>"
                f"<p style='margin:28px 0'><a href='{html.escape(verify_url)}' style='background:#ffcf4a;color:#17213b;"
                "padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:bold;font-size:17px;"
                "display:inline-block'>确认订阅</a></p>"
                "<p style='color:#667085;font-size:14px'>按钮在 24 小时内有效，只能使用一次。按钮打不开的话，复制这个链接到浏览器：<br>"
                f"<span style='word-break:break-all'>{html.escape(verify_url)}</span></p>"
                "<p style='color:#667085;font-size:14px'>如果不是你本人提交的，忽略这封邮件即可，不会产生任何订阅。</p></main>")
        try:
            provider_id = self.mailer.send(email.strip(), "还差一步：确认你的 PNU Notice 订阅", body, f"verification-{digest}")
            with self.db.transaction() as conn:
                conn.execute("UPDATE notification_deliveries SET status='sent',sent_at=?,provider_message_id=? WHERE id=?",
                             (iso_utc(), provider_id, delivery_id))
        except Exception as exc:
            with self.db.transaction() as conn:
                conn.execute("UPDATE notification_deliveries SET status='failed',error=?,attempts=1 WHERE id=?",
                             (str(exc)[:2000], delivery_id))
            raise
        return poll_token

    def confirmation_status(self, poll_token: str) -> dict | None:
        """Whether the subscription submitted with this poll token has been confirmed (possibly in another browser).
        Only the submitting page holds the token, so this reveals nothing about other addresses."""
        if not poll_token:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT s.id,s.email,s.status,s.verified_at,v.created_at FROM email_verifications v
                   JOIN subscribers s ON s.id=v.subscriber_id WHERE v.poll_token_hash=?""",
                (token_hash(poll_token, self.settings.app_secret),)).fetchone()
        if not row:
            return None
        confirmed = bool(row["verified_at"]) and row["verified_at"] >= row["created_at"] and row["status"] == "active"
        return {"subscriber_id": row["id"], "email": row["email"], "confirmed": confirmed}

    def verify(self, token: str) -> str:
        digest = token_hash(token, self.settings.app_secret)
        now = iso_utc()
        with self.db.transaction() as conn:
            verification = conn.execute(
                """SELECT * FROM email_verifications WHERE token_hash=? AND used_at IS NULL AND expires_at>?""",
                (digest, now),
            ).fetchone()
            if not verification:
                raise ValueError("验证链接无效、已使用或已过期。")
            subscriber_id = verification["subscriber_id"]
            subscriber = conn.execute("SELECT * FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
            newly_active = subscriber["status"] != "active"
            # Keep an existing management link working so links in earlier emails stay valid.
            created = subscriber["management_token_created_at"] or now
            raw_management = management_token(subscriber_id, created, self.settings.app_secret)
            management_hash = token_hash(raw_management, self.settings.app_secret)
            conn.execute("UPDATE email_verifications SET used_at=? WHERE id=?", (now, verification["id"]))
            conn.execute("""UPDATE subscribers SET status='active',verified_at=?,unsubscribed_at=NULL,
                            verification_token_hash=NULL,verification_expires_at=NULL,
                            management_token_hash=?,management_token_created_at=? WHERE id=?""",
                         (now, management_hash, created, subscriber_id))
            preferences = json.loads(verification["preferences_json"])
            if preferences.get("sources"):
                conn.execute("DELETE FROM subscriptions WHERE subscriber_id=?", (subscriber_id,))
                conn.executemany(
                    """INSERT INTO subscriptions(subscriber_id,source_key,enabled,immediate_enabled,daily_digest_enabled,
                       weekly_digest_enabled,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?)""",
                    [(subscriber_id, key, int(preferences["immediate"]), int(preferences["daily"]),
                      int(preferences["weekly"]), now, now) for key in preferences["sources"] if key in SOURCE_BY_KEY],
                )
        if newly_active:
            self.send_welcome(subscriber_id, now)
        return raw_management

    def send_welcome(self, subscriber_id: int, verified_at: str) -> None:
        """Welcome email on first activation (or re-activation). A failure is logged and never undoes the subscription."""
        subscriber, subscriptions = self.get(subscriber_id)
        now = iso_utc()
        with self.db.transaction() as conn:
            delivery_id = conn.execute(
                """INSERT OR IGNORE INTO notification_deliveries(subscriber_id,notification_type,dedupe_key,scheduled_at,
                   status,created_at) VALUES(?,'welcome',?,?,'sending',?)""",
                (subscriber_id, f"welcome:{verified_at}", now, now)).lastrowid
        if not delivery_id:
            return
        subject, body = welcome_email(self.settings, subscriber, subscriptions)
        try:
            provider_id = self.mailer.send(subscriber["email"], subject, body, f"welcome-{subscriber_id}-{verified_at}"[:250])
            with self.db.transaction() as conn:
                conn.execute("UPDATE notification_deliveries SET status='sent',sent_at=?,provider_message_id=? WHERE id=?",
                             (iso_utc(), provider_id, delivery_id))
        except Exception as exc:
            with self.db.transaction() as conn:
                conn.execute("UPDATE notification_deliveries SET status='failed',error=?,attempts=1 WHERE id=?",
                             (str(exc)[:2000], delivery_id))
                conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                             ("welcome_email", None, str(exc)[:2000], iso_utc()))

    def get_by_management_token(self, token: str):
        digest = token_hash(token, self.settings.app_secret)
        with self.db.connect() as conn:
            subscriber = conn.execute("SELECT * FROM subscribers WHERE management_token_hash=?", (digest,)).fetchone()
        if not subscriber:
            raise ValueError("管理链接无效或已撤销。")
        return self.get(subscriber["id"])

    def get(self, subscriber_id: int):
        with self.db.connect() as conn:
            subscriber = conn.execute("SELECT * FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
            if not subscriber:
                raise ValueError("订阅不存在。")
            subscriptions = conn.execute("SELECT * FROM subscriptions WHERE subscriber_id=?", (subscriber_id,)).fetchall()
            return dict(subscriber), [dict(item) for item in subscriptions]

    def management_link(self, subscriber: dict) -> str | None:
        if not subscriber.get("management_token_created_at"):
            return None
        token = management_token(subscriber["id"], subscriber["management_token_created_at"], self.settings.app_secret)
        return f"/subscription/manage?token={token}"

    def update(self, token: str, source_keys: list[str], immediate: bool, daily: bool, weekly: bool,
               action: str = "save") -> None:
        subscriber, _ = self.get_by_management_token(token)
        self.update_subscriber(subscriber["id"], source_keys, immediate, daily, weekly, action)

    def update_subscriber(self, subscriber_id: int, source_keys: list[str], immediate: bool, daily: bool,
                          weekly: bool, action: str = "save") -> None:
        subscriber, _ = self.get(subscriber_id)
        now = iso_utc()
        with self.db.transaction() as conn:
            if action == "unsubscribe":
                conn.execute("UPDATE subscribers SET status='unsubscribed',unsubscribed_at=? WHERE id=?", (now, subscriber["id"]))
                return
            if action == "pause":
                conn.execute("UPDATE subscribers SET status='paused' WHERE id=?", (subscriber["id"],))
                return
            if action == "resume":
                conn.execute("UPDATE subscribers SET status='active',unsubscribed_at=NULL WHERE id=?", (subscriber["id"],))
                return
            selected = list(dict.fromkeys(source_keys))
            if not selected or any(key not in SOURCE_BY_KEY for key in selected):
                raise ValueError("请至少选择一个公告来源。")
            if not any((immediate, daily, weekly)):
                raise ValueError("请至少选择一种通知频率。")
            for key in SOURCE_BY_KEY:
                conn.execute("""INSERT INTO subscriptions(subscriber_id,source_key,enabled,immediate_enabled,
                                daily_digest_enabled,weekly_digest_enabled,created_at,updated_at)
                                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subscriber_id,source_key) DO UPDATE SET
                                enabled=excluded.enabled,immediate_enabled=excluded.immediate_enabled,
                                daily_digest_enabled=excluded.daily_digest_enabled,
                                weekly_digest_enabled=excluded.weekly_digest_enabled,updated_at=excluded.updated_at""",
                             (subscriber["id"], key, int(key in selected), int(immediate), int(daily), int(weekly), now, now))
