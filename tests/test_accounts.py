from __future__ import annotations

import io
import re
import tempfile
import unittest
from dataclasses import replace
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode

from pnu_notice.accounts import AccountService, check_password, hash_password
from pnu_notice.config import Settings
from pnu_notice.db import Database
from pnu_notice.sources import SOURCES
from pnu_notice.subscriptions import SubscriptionService
from pnu_notice.timeutil import iso_utc
from pnu_notice.web import WebApp


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body_html, idempotency_key):
        self.sent.append((to, subject, body_html))
        return "email_123"

    def last_link(self, path):
        return re.search(rf"{path}\?token=([^'\"&]+)", self.sent[-1][2]).group(1)


class Client:
    def __init__(self, app):
        self.app = app
        self.cookies = {}

    def request(self, method, path, data=None):
        path, _, query = path.partition("?")
        body = urlencode(data or {}, doseq=True).encode()
        environ = {"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": method,
                   "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
                   "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        captured = {}

        def start_response(status, headers):
            captured["status"], captured["headers"] = int(status.split()[0]), headers

        page = b"".join(self.app(environ, start_response)).decode()
        for name, value in captured["headers"]:
            if name == "Set-Cookie":
                cookie = SimpleCookie(value)
                for key, morsel in cookie.items():
                    if morsel["max-age"] == "0":
                        self.cookies.pop(key, None)
                    else:
                        self.cookies[key] = morsel.value
        location = dict(captured["headers"]).get("Location")
        return captured["status"], page, location

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, data):
        return self.request("POST", path, data)

    def csrf(self, path="/account"):
        return re.search(r'name="csrf" value="([0-9a-f]+)"', self.get(path)[1]).group(1)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings.from_env(), database_path=Path(self.temp.name) / "test.sqlite3",
                                app_secret="test-secret-that-is-long-enough", app_base_url="https://notice.example.test",
                                turnstile_secret=None)
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        with self.db.transaction() as conn:
            for source in SOURCES:
                conn.execute("INSERT INTO sources(source_key,name,page_url,rss_url,created_at) VALUES(?,?,?,?,?)",
                             (source.key, source.name, source.page_url, source.rss_url, iso_utc()))
        self.mailer = FakeMailer()
        self.subscriptions = SubscriptionService(self.db, self.settings, self.mailer)
        self.accounts = AccountService(self.db, self.settings, self.mailer)
        self.client = Client(WebApp(self.subscriptions, self.accounts))

    def tearDown(self):
        self.temp.cleanup()

    def _register(self, email="student@example.com"):
        self.client.post("/subscribe", {"email": email, "source": [SOURCES[0].key], "daily": "on"})
        status, _, location = self.client.get(f"/verify?token={self.mailer.last_link('/verify')}")
        self.assertEqual((status, location), (303, "/account?welcome=1"))

    def test_password_hash_round_trip(self):
        stored = hash_password("correct horse")
        self.assertTrue(check_password("correct horse", stored))
        self.assertFalse(check_password("wrong horse", stored))
        self.assertNotIn("correct horse", stored)

    def test_verify_signs_in_then_user_sets_password_and_logs_back_in(self):
        self._register()
        status, page, _ = self.client.get("/account?welcome=1")
        self.assertEqual(status, 200)
        self.assertIn("设置登录密码", page)
        self.assertNotIn("当前密码", page)
        csrf = self.client.csrf()
        status, _, location = self.client.post("/account/password", {
            "csrf": csrf, "new_password": "pnu-notice-2026", "confirm_password": "pnu-notice-2026"})
        self.assertEqual((status, location), (303, "/account?password=1"))
        self.client.post("/logout", {"csrf": self.client.csrf()})
        self.assertEqual(self.client.get("/account")[2], "/login")
        status, _, location = self.client.post("/login", {"email": "Student@Example.com", "password": "pnu-notice-2026"})
        self.assertEqual((status, location), (303, "/account"))
        self.assertIn("我的订阅", self.client.get("/account")[1])

    def test_logged_in_user_updates_subscription(self):
        self._register()
        self.client.post("/account", {"csrf": self.client.csrf(), "action": "save",
                                      "source": [SOURCES[1].key, SOURCES[2].key], "weekly": "on"})
        with self.db.connect() as conn:
            enabled = [row[0] for row in conn.execute("SELECT source_key FROM subscriptions WHERE enabled=1 ORDER BY source_key")]
        self.assertEqual(enabled, sorted([SOURCES[1].key, SOURCES[2].key]))
        self.client.post("/account", {"csrf": self.client.csrf(), "action": "unsubscribe"})
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM subscribers").fetchone()[0], "unsubscribed")

    def test_post_without_csrf_is_rejected(self):
        self._register()
        status, _, _ = self.client.post("/account", {"action": "unsubscribe"})
        self.assertEqual(status, 403)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM subscribers").fetchone()[0], "active")

    def test_repeated_wrong_passwords_lock_the_account(self):
        self._register()
        self.client.post("/account/password", {"csrf": self.client.csrf(), "new_password": "pnu-notice-2026",
                                               "confirm_password": "pnu-notice-2026"})
        other = Client(self.client.app)
        for _ in range(5):
            self.assertEqual(other.post("/login", {"email": "student@example.com", "password": "nope"})[0], 401)
        status, page, _ = other.post("/login", {"email": "student@example.com", "password": "pnu-notice-2026"})
        self.assertEqual(status, 401)
        self.assertIn("后可以再试", page)

    def test_forgot_password_sets_password_once(self):
        self._register()
        self.client.cookies.clear()
        sent_before = len(self.mailer.sent)
        self.client.post("/forgot-password", {"email": "student@example.com"})
        self.assertEqual(len(self.mailer.sent), sent_before + 1)
        token = self.mailer.last_link("/reset-password")
        self.assertEqual(self.client.get(f"/reset-password?token={token}")[0], 200)
        status, _, location = self.client.post("/reset-password", {
            "token": token, "new_password": "new-pass-2026", "confirm_password": "new-pass-2026"})
        self.assertEqual((status, location), (303, "/account?password=1"))
        self.assertEqual(self.client.get(f"/reset-password?token={token}")[0], 400)
        self.assertIsNotNone(self.accounts.authenticate("student@example.com", "new-pass-2026"))

    def test_forgot_password_does_not_reveal_unknown_email(self):
        status, page, _ = self.client.post("/forgot-password", {"email": "nobody@example.com"})
        self.assertEqual(status, 200)
        self.assertIn("已提交", page)
        self.assertEqual(self.mailer.sent, [])

    def test_admin_must_change_initial_password_and_cannot_reset_by_email(self):
        self.accounts.create_admin("weekend2000119@gmail.com", "123456")
        status, _, location = self.client.post("/login", {"email": "weekend2000119@gmail.com", "password": "123456"})
        self.assertEqual((status, location), (303, "/account"))
        self.assertEqual(self.client.get("/admin")[2], "/account")
        self.assertIn("修改初始密码", self.client.get("/account")[1])
        status, page, _ = self.client.post("/account/password", {
            "csrf": self.client.csrf(), "current_password": "123456",
            "new_password": "123456", "confirm_password": "123456"})
        self.assertEqual(status, 400)
        self.client.post("/account/password", {"csrf": self.client.csrf(), "current_password": "123456",
                                               "new_password": "admin-pass-2026", "confirm_password": "admin-pass-2026"})
        status, page, _ = self.client.get("/admin")
        self.assertEqual(status, 200)
        self.assertIn("公告来源", page)
        self.assertEqual(self.client.get("/admin/subscribers")[0], 200)
        self.assertEqual(self.client.get("/admin/notices")[0], 200)
        self.client.post("/forgot-password", {"email": "weekend2000119@gmail.com"})
        self.assertEqual(self.mailer.sent, [])

    def test_regular_user_cannot_open_admin_and_admin_can_pause_subscriber(self):
        self._register()
        self.assertEqual(self.client.get("/admin")[2], "/login")
        self.accounts.create_admin("admin@example.com", "123456")
        admin = Client(self.client.app)
        admin.post("/login", {"email": "admin@example.com", "password": "123456"})
        admin.post("/account/password", {"csrf": admin.csrf(), "current_password": "123456",
                                         "new_password": "admin-pass-2026", "confirm_password": "admin-pass-2026"})
        with self.db.connect() as conn:
            subscriber_id = conn.execute("SELECT id FROM subscribers").fetchone()[0]
        admin.post("/admin/subscribers", {"csrf": admin.csrf("/admin/subscribers"), "id": subscriber_id, "action": "pause"})
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM subscribers").fetchone()[0], "paused")

    def test_signed_in_user_is_sent_to_account_instead_of_old_pages(self):
        self._register()
        self.assertEqual(self.client.get("/subscribe")[2], "/account")
        subscriber, _ = self.subscriptions.get(1)
        self.assertEqual(self.client.get(self.subscriptions.management_link(subscriber))[2], "/account")
        status, page, _ = Client(self.client.app).get(self.subscriptions.management_link(subscriber))
        self.assertEqual(status, 200)
        self.assertIn("无需登录", page)


    def test_submitting_shows_check_email_step_and_resend_keeps_user_there(self):
        status, page, _ = self.client.post("/subscribe", {"email": "someone@gmail.com", "source": [SOURCES[0].key],
                                                          "daily": "on"})
        self.assertEqual(status, 200)
        self.assertIn("还差一步", page)
        self.assertIn("someone@gmail.com", page)
        self.assertIn("https://mail.google.com/", page)
        self.assertIn('name="resend"', page)
        self.assertEqual(len(self.mailer.sent), 1)
        status, page, _ = self.client.post("/subscribe", {"email": "someone@gmail.com", "source": [SOURCES[0].key],
                                                          "daily": "on", "resend": "1"})
        self.assertEqual(status, 400)
        self.assertIn("还差一步", page)
        self.assertIn("刚刚已经发送", page)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertNotIn("subscriber", page.lower())


    def test_original_tab_learns_about_confirmation_done_in_another_browser(self):
        status, page, _ = self.client.post("/subscribe", {"email": "a@example.com", "source": [SOURCES[0].key], "daily": "on"})
        poll = re.search(r'data-token="([^"]+)"', page).group(1)
        self.assertIn("/static/check-email.js", page)
        self.assertEqual(self.client.get("/static/check-email.js")[0], 200)
        self.assertEqual(self.client.get(f"/subscribe/status?t={poll}")[1], '{"confirmed": false}')
        self.assertIn("还没有完成确认", self.client.get(f"/subscribe/done?t={poll}")[1])
        mail_app = Client(self.client.app)
        mail_app.get(f"/verify?token={self.mailer.last_link('/verify')}")
        self.assertEqual(self.client.get(f"/subscribe/status?t={poll}")[1], '{"confirmed": true}')
        status, page, _ = self.client.get(f"/subscribe/done?t={poll}")
        self.assertIn("订阅已生效", page)
        self.assertNotIn("pnu_session", self.client.cookies)  # the original tab is told, not signed in
        self.assertEqual(mail_app.get(f"/subscribe/done?t={poll}")[2], "/account")
        self.assertEqual(self.client.get("/subscribe/status?t=unknown")[0], 404)


    def _admin_client(self):
        self.accounts.create_admin("admin@example.com", "123456")
        admin = Client(self.client.app)
        admin.post("/login", {"email": "admin@example.com", "password": "123456"})
        admin.post("/account/password", {"csrf": admin.csrf(), "current_password": "123456",
                                         "new_password": "admin-pass-2026", "confirm_password": "admin-pass-2026"})
        return admin

    def test_admin_notice_list_detail_and_reanalyze(self):
        from pnu_notice.crawler import Crawler, NoticeDetail
        from datetime import datetime
        from pnu_notice.timeutil import SEOUL
        detail = NoticeDetail("77", "장학금 신청 안내", "https://cse.pusan.ac.kr/bbs/cse/2055/77/artclView.do", "작성자",
                              datetime(2026, 10, 1, tzinfo=SEOUL), "장학", "<p>본문</p>", "본문 내용입니다", [])
        _, notice_id = Crawler(self.db, self.settings).save(SOURCES[0], detail, False)
        with self.db.transaction() as conn:
            content_hash = conn.execute("SELECT content_hash FROM notices").fetchone()[0]
            conn.execute("INSERT INTO ai_analyses(notice_id,content_hash,result_json,model,created_at) VALUES(?,?,?,?,?)",
                         (notice_id, content_hash, '{"title_zh":"奖学金申请通知","summary_zh":"摘要内容","importance":"high",'
                          '"deadlines":[],"actions":[],"eligibility":[],"required_documents":[],"warnings":[],"status":"active"}',
                          "test", iso_utc()))
            conn.execute("UPDATE notices SET ai_status='completed'")
        admin = self._admin_client()
        status, page, _ = admin.get("/admin/notices?q=奖学金")
        self.assertEqual(status, 200)
        self.assertIn("奖学金申请通知", page)
        self.assertIn("장학금 신청 안내", page)
        self.assertNotIn("奖学金申请通知", admin.get("/admin/notices?q=没有这个")[1])
        self.assertIn("没有符合条件", admin.get("/admin/notices?scope=new&ai=queued")[1])
        status, page, _ = admin.get(f"/admin/notices/{notice_id}")
        self.assertEqual(status, 200)
        self.assertIn("摘要内容", page)
        self.assertIn("data-confirm", page)
        status, page, _ = admin.post(f"/admin/notices/{notice_id}", {"csrf": admin.csrf(f"/admin/notices/{notice_id}"),
                                                                    "action": "reanalyze"})
        self.assertIn("已加入 AI 分析队列", page)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT ai_status FROM notices").fetchone()[0], "pending")
        self.assertEqual(admin.post(f"/admin/notices/{notice_id}", {"action": "reanalyze"})[0], 403)
        self.assertEqual(admin.get("/admin/notices/999")[0], 404)
        self.assertEqual(admin.get("/static/admin.js")[0], 200)
        self.assertEqual(admin.get("/admin")[0], 200)

    def test_admin_subscriber_filters_and_search(self):
        self._register("one@example.com")
        self.client.cookies.clear()
        self._register("two@example.com")
        admin = self._admin_client()
        page = admin.get("/admin/subscribers?q=two")[1]
        self.assertIn("two@example.com", page)
        self.assertNotIn("one@example.com", page)
        self.assertIn("没有符合条件", admin.get("/admin/subscribers?status=paused")[1])


    def _person_rows(self, email):
        with self.db.connect() as conn:
            subscriber = conn.execute("SELECT id FROM subscribers WHERE email_normalized=?", (email,)).fetchone()
            sid = subscriber[0] if subscriber else -1
            return {
                "subscribers": conn.execute("SELECT COUNT(*) FROM subscribers WHERE email_normalized=?", (email,)).fetchone()[0],
                "accounts": conn.execute("SELECT COUNT(*) FROM accounts WHERE email_normalized=?", (email,)).fetchone()[0],
                "subscriptions": conn.execute("SELECT COUNT(*) FROM subscriptions WHERE subscriber_id=?", (sid,)).fetchone()[0],
                "deliveries": conn.execute("SELECT COUNT(*) FROM notification_deliveries WHERE subscriber_id=?", (sid,)).fetchone()[0],
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            }

    def test_user_deletes_own_account_with_password_and_everything_is_gone(self):
        self._register("gone@example.com")
        self.client.post("/account/password", {"csrf": self.client.csrf(), "new_password": "pnu-notice-2026",
                                               "confirm_password": "pnu-notice-2026"})
        self.assertIn("永久注销", self.client.get("/account")[1])
        status, page, _ = self.client.post("/account/delete", {"csrf": self.client.csrf(), "current_password": "wrong"})
        self.assertEqual(status, 400)
        self.assertEqual(self._person_rows("gone@example.com")["accounts"], 1)
        self.assertEqual(self.client.post("/account/delete", {"current_password": "pnu-notice-2026"})[0], 403)  # no CSRF
        status, _, location = self.client.post("/account/delete", {"csrf": self.client.csrf(),
                                                                   "current_password": "pnu-notice-2026"})
        self.assertEqual((status, location), (303, "/?deleted=1"))
        self.assertNotIn("pnu_session", self.client.cookies)
        self.assertEqual(self._person_rows("gone@example.com"),
                         {"subscribers": 0, "accounts": 0, "subscriptions": 0, "deliveries": 0, "sessions": 0})
        self.assertIn("永久删除", self.client.get("/?deleted=1")[1])
        self._register("gone@example.com")  # the address can subscribe again from scratch

    def test_passwordless_user_confirms_deletion_by_typing_email(self):
        self._register("typed@example.com")
        status, _, _ = self.client.post("/account/delete", {"csrf": self.client.csrf(), "confirm_email": "other@example.com"})
        self.assertEqual(status, 400)
        status, _, location = self.client.post("/account/delete", {"csrf": self.client.csrf(),
                                                                   "confirm_email": " Typed@Example.com "})
        self.assertEqual(location, "/?deleted=1")
        self.assertEqual(self._person_rows("typed@example.com")["subscribers"], 0)

    def test_admin_deletes_subscriber_but_never_an_admin(self):
        self._register("victim@example.com")
        admin = self._admin_client()
        with self.db.connect() as conn:
            subscriber_id = conn.execute("SELECT id FROM subscribers WHERE email_normalized='victim@example.com'").fetchone()[0]
            conn.execute("""INSERT INTO notification_deliveries(subscriber_id,notification_type,dedupe_key,scheduled_at,
                            status,created_at) VALUES(?,'daily_digest','x',?,'pending',?)""", (subscriber_id, iso_utc(), iso_utc()))
            conn.commit()
        page = admin.get("/admin/subscribers")[1]
        self.assertIn('value="delete"', page)
        status, page, _ = admin.post("/admin/subscribers", {"csrf": admin.csrf("/admin/subscribers"), "id": subscriber_id,
                                                            "action": "delete"})
        self.assertIn("已永久删除 victim@example.com", page)
        self.assertEqual(self._person_rows("victim@example.com")["deliveries"], 0)
        self.assertEqual(self._person_rows("victim@example.com")["subscribers"], 0)
        with self.assertRaises(ValueError):
            self.accounts.delete_person(account_id=self.accounts.authenticate("admin@example.com", "admin-pass-2026")["id"])
        self.assertNotIn("永久注销", admin.get("/account")[1])



if __name__ == "__main__":
    unittest.main()
