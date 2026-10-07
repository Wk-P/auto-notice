from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
from datetime import datetime, timedelta

from .config import Settings
from .db import Database
from .emailer import ResendMailer
from .security import random_token, token_hash
from .timeutil import SEOUL, iso_utc, now_utc

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
USER_SESSION_TTL = timedelta(days=30)
ADMIN_SESSION_TTL = timedelta(hours=12)
RESET_TTL = timedelta(hours=1)
RESET_COOLDOWN = timedelta(minutes=5)
RESET_DAILY_LIMIT = 2  # reset emails per address per Seoul calendar day
WEAK_PASSWORDS = {"12345678", "123456789", "1234567890", "password", "password1", "qwertyui", "11111111", "00000000"}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    encode = lambda value: base64.b64encode(value).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Spend the same work for unknown accounts so response time does not reveal which emails exist.
        hash_password(password)
        return False
    _, n, r, p, salt, expected = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    return hmac.compare_digest(digest, base64.b64decode(expected))


def _locked_message(locked_until: str) -> str:
    unlock = datetime.fromisoformat(locked_until).astimezone(SEOUL).strftime("%H:%M")
    return (f"密码连续输错 {MAX_FAILED_LOGINS} 次，账户已暂时锁定，首尔时间 {unlock} 后可以再试。"
            "如果忘了密码，请使用“忘记密码”。")


def validate_new_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 位。")
    if len(password) > 128:
        raise ValueError("密码不能超过 128 位。")
    if password.lower() in WEAK_PASSWORDS or len(set(password)) < 3:
        raise ValueError("密码太简单，请换一个。")


class AccountService:
    def __init__(self, db: Database, settings: Settings, mailer: ResendMailer):
        self.db = db
        self.settings = settings
        self.mailer = mailer

    def _hash(self, token: str) -> str:
        return token_hash(token, self.settings.app_secret)

    def get(self, account_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def ensure_for_subscriber(self, subscriber: dict) -> dict:
        """Called after the mailbox owner clicked a verification link, so the email is proven."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE email_normalized=?", (subscriber["email_normalized"],)).fetchone()
            if row is None:
                account_id = conn.execute(
                    "INSERT INTO accounts(email,email_normalized,subscriber_id,role,created_at) VALUES(?,?,?,'user',?)",
                    (subscriber["email"], subscriber["email_normalized"], subscriber["id"], iso_utc())).lastrowid
            else:
                account_id = row["id"]
                if row["subscriber_id"] is None:
                    conn.execute("UPDATE accounts SET subscriber_id=? WHERE id=?", (subscriber["id"], account_id))
        return self.get(account_id)

    def create_admin(self, email: str, password: str) -> dict:
        """Bootstrap or reset an administrator from the server shell; the password must be changed at first login."""
        normalized = email.strip().casefold()
        if not password:
            raise ValueError("密码不能为空。")
        now = iso_utc()
        with self.db.transaction() as conn:
            subscriber = conn.execute("SELECT id FROM subscribers WHERE email_normalized=?", (normalized,)).fetchone()
            conn.execute(
                """INSERT INTO accounts(email,email_normalized,subscriber_id,role,password_hash,must_change_password,
                   password_changed_at,created_at) VALUES(?,?,?,'admin',?,1,?,?)
                   ON CONFLICT(email_normalized) DO UPDATE SET role='admin',password_hash=excluded.password_hash,
                   must_change_password=1,password_changed_at=excluded.password_changed_at,failed_logins=0,
                   locked_until=NULL""",
                (email.strip(), normalized, subscriber["id"] if subscriber else None, hash_password(password), now, now))
            account = conn.execute("SELECT * FROM accounts WHERE email_normalized=?", (normalized,)).fetchone()
            conn.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
        return dict(account)

    def authenticate(self, email: str, password: str) -> dict:
        normalized = email.strip().casefold()
        now = now_utc()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE email_normalized=?", (normalized,)).fetchone()
        if row and row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
            raise ValueError(_locked_message(row["locked_until"]))
        if row and check_password(password, row["password_hash"]):
            with self.db.transaction() as conn:
                conn.execute("UPDATE accounts SET failed_logins=0,locked_until=NULL,last_login_at=? WHERE id=?",
                             (iso_utc(), row["id"]))
            return self.get(row["id"])
        if row is None:
            check_password(password, None)
        else:
            failures = row["failed_logins"] + 1
            locked = iso_utc(now + LOCKOUT) if failures >= MAX_FAILED_LOGINS else None
            with self.db.transaction() as conn:
                conn.execute("UPDATE accounts SET failed_logins=?,locked_until=? WHERE id=?",
                             (0 if locked else failures, locked, row["id"]))
            if locked:
                raise ValueError(_locked_message(locked))
        raise ValueError("邮箱或密码错误。没有设置过密码的话，请使用“忘记密码”。")

    def create_session(self, account: dict) -> str:
        token = random_token()
        ttl = ADMIN_SESSION_TTL if account["role"] == "admin" else USER_SESSION_TTL
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (iso_utc(),))
            conn.execute("INSERT INTO sessions(account_id,token_hash,created_at,expires_at) VALUES(?,?,?,?)",
                         (account["id"], self._hash(token), iso_utc(), iso_utc(now_utc() + ttl)))
        return token

    def session_account(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id
                   WHERE s.token_hash=? AND s.expires_at>?""", (self._hash(token), iso_utc())).fetchone()
        return dict(row) if row else None

    def logout(self, token: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (self._hash(token),))

    def csrf_token(self, session_token: str) -> str:
        return hmac.new(self.settings.app_secret.encode(), f"csrf:{session_token}".encode(), hashlib.sha256).hexdigest()[:32]

    def check_csrf(self, session_token: str, submitted: str) -> bool:
        return hmac.compare_digest(self.csrf_token(session_token), submitted or "")

    def change_password(self, account: dict, new_password: str, current_password: str | None) -> str:
        """Sets a new password, signs out every other session, and returns a fresh session token."""
        if account["password_hash"] and not check_password(current_password or "", account["password_hash"]):
            raise ValueError("当前密码不正确。")
        validate_new_password(new_password)
        if account["password_hash"] and check_password(new_password, account["password_hash"]):
            raise ValueError("新密码不能与当前密码相同。")
        with self.db.transaction() as conn:
            conn.execute("""UPDATE accounts SET password_hash=?,must_change_password=0,password_changed_at=?,
                            failed_logins=0,locked_until=NULL WHERE id=?""",
                         (hash_password(new_password), iso_utc(), account["id"]))
            conn.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
        return self.create_session(self.get(account["id"]))

    def request_password_reset(self, email: str) -> None:
        """Always behaves the same for the caller, so it cannot be used to discover registered emails.
        Administrators reset from the server shell instead, so a compromised mailbox cannot take over the admin."""
        normalized = email.strip().casefold()
        now = now_utc()
        with self.db.connect() as conn:
            account = conn.execute("SELECT role FROM accounts WHERE email_normalized=?", (normalized,)).fetchone()
            subscriber = conn.execute(
                """SELECT email FROM subscribers WHERE email_normalized=? AND verified_at IS NOT NULL
                   AND status!='email_invalid'""", (normalized,)).fetchone()
            recent = conn.execute("SELECT 1 FROM password_resets WHERE email_normalized=? AND created_at>?",
                                  (normalized, iso_utc(now - RESET_COOLDOWN))).fetchone()
            today = datetime.combine(now.astimezone(SEOUL).date(), datetime.min.time(), SEOUL)
            sent_today = conn.execute("SELECT COUNT(*) FROM password_resets WHERE email_normalized=? AND created_at>=?",
                                      (normalized, iso_utc(today))).fetchone()[0]
        # Over the limit looks exactly like an unknown address, so the response never reveals which emails exist.
        if (account and account["role"] == "admin") or subscriber is None or recent or sent_today >= RESET_DAILY_LIMIT:
            return
        token = random_token()
        with self.db.transaction() as conn:
            conn.execute("UPDATE password_resets SET used_at=? WHERE email_normalized=? AND used_at IS NULL",
                         (iso_utc(), normalized))
            conn.execute("INSERT INTO password_resets(email_normalized,token_hash,expires_at,created_at) VALUES(?,?,?,?)",
                         (normalized, self._hash(token), iso_utc(now + RESET_TTL), iso_utc()))
        link = f"{self.settings.app_base_url}/reset-password?token={token}"
        body = ("<main style='font-family:Arial,sans-serif;max-width:620px;margin:auto'>"
                "<h1>设置你的 PNU Notice 密码</h1><p>点击下面的链接设置新密码。链接 1 小时内有效，且只能使用一次。</p>"
                f"<p><a href='{html.escape(link)}'>设置新密码</a></p>"
                "<p>如果不是你本人操作，请忽略此邮件，你的密码不会改变。</p></main>")
        self.mailer.send(subscriber["email"], "设置你的 PNU Notice 密码", body, f"password-reset-{self._hash(token)[:40]}")

    def _valid_reset(self, conn, token: str):
        return conn.execute("SELECT * FROM password_resets WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                            (self._hash(token), iso_utc())).fetchone()

    def reset_token_valid(self, token: str) -> bool:
        with self.db.connect() as conn:
            return self._valid_reset(conn, token) is not None

    def reset_password(self, token: str, new_password: str) -> str:
        validate_new_password(new_password)
        with self.db.transaction() as conn:
            reset = self._valid_reset(conn, token)
            if not reset:
                raise ValueError("链接无效、已使用或已过期，请重新申请。")
            conn.execute("UPDATE password_resets SET used_at=? WHERE id=?", (iso_utc(), reset["id"]))
            subscriber = conn.execute("SELECT * FROM subscribers WHERE email_normalized=?",
                                      (reset["email_normalized"],)).fetchone()
        account = self.ensure_for_subscriber(dict(subscriber))
        if account["role"] == "admin":
            raise ValueError("管理员请在服务器上重置密码。")
        with self.db.transaction() as conn:
            conn.execute("""UPDATE accounts SET password_hash=?,must_change_password=0,password_changed_at=?,
                            failed_logins=0,locked_until=NULL WHERE id=?""",
                         (hash_password(new_password), iso_utc(), account["id"]))
            conn.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
        return self.create_session(self.get(account["id"]))

    def delete_person(self, subscriber_id: int | None = None, account_id: int | None = None) -> str:
        """Permanently removes everything stored about one person: subscriber, subscriptions, verifications,
        delivery history (including pending jobs), account, sessions and reset tokens. Returns the email removed.
        Administrators are refused so the site can never be left without one."""
        with self.db.transaction() as conn:
            account = (conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone() if account_id
                       else conn.execute("SELECT * FROM accounts WHERE subscriber_id=?", (subscriber_id,)).fetchone())
            if account and account["role"] == "admin":
                raise ValueError("管理员账户不能删除。")
            subscriber_id = subscriber_id or (account["subscriber_id"] if account else None)
            subscriber = (conn.execute("SELECT * FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
                          if subscriber_id else None)
            if not account and not subscriber:
                raise ValueError("用户不存在。")
            email = (subscriber or account)["email"]
            normalized = (subscriber or account)["email_normalized"]
            if account:
                conn.execute("DELETE FROM sessions WHERE account_id=?", (account["id"],))
                conn.execute("DELETE FROM accounts WHERE id=?", (account["id"],))
            if subscriber:
                for table in ("notification_deliveries", "email_verifications", "subscriptions"):
                    conn.execute(f"DELETE FROM {table} WHERE subscriber_id=?", (subscriber["id"],))
                conn.execute("DELETE FROM subscribers WHERE id=?", (subscriber["id"],))
            conn.execute("DELETE FROM password_resets WHERE email_normalized=?", (normalized,))
        return email
