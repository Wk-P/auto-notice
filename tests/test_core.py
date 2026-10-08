from __future__ import annotations

import tempfile
import unittest
import io
import json
from unittest import mock
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from pnu_notice.ai import AIWorker, OpenAIAnalyzer
from pnu_notice.config import Settings
from pnu_notice.crawler import Attachment, Crawler, DiscoveredNotice, NoticeDetail, parse_detail, parse_list_page, parse_rss
from pnu_notice.db import Database
from pnu_notice.delivery import (DeliveryPlanner, DeliveryWorker, ReminderPlanner, is_student_deadline,
                                 next_digest_slot, reminder_time)
from pnu_notice.sources import SOURCES
from pnu_notice.subscriptions import SubscriptionService
from pnu_notice.web import WebApp
from pnu_notice.timeutil import SEOUL, iso_utc, parse_datetime


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body_html, idempotency_key):
        self.sent.append((to, subject, body_html, idempotency_key))
        return "email_123"


class PnuNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(
            Settings.from_env(),
            database_path=Path(self.temp.name) / "test.sqlite3",
            app_secret="test-secret-that-is-long-enough",
            app_base_url="https://notice.example.test",
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        with self.db.transaction() as conn:
            for source in SOURCES:
                conn.execute("INSERT INTO sources(source_key,name,page_url,rss_url,created_at) VALUES(?,?,?,?,?)",
                             (source.key, source.name, source.page_url, source.rss_url, iso_utc()))

    def tearDown(self):
        self.temp.cleanup()

    def test_rss_parser_uses_stable_article_id(self):
        xml = """<?xml version="1.0"?><rss><channel><item><title>공지</title>
        <link>https://cse.pusan.ac.kr/bbs/cse/2055/1464728/artclView.do</link>
        <guid>notice-1464728</guid><pubDate>Wed, 30 Sep 2026 12:00:00 +0900</pubDate></item></channel></rss>"""
        items = parse_rss(xml)
        self.assertEqual(items[0].external_id, "1464728")
        self.assertEqual(items[0].published_at.year, 2026)

    def test_rss_parser_resolves_relative_link(self):
        xml = """<rss><channel><item><title>공지</title>
        <link>/bbs/cse/2055/42/artclView.do</link><guid>42</guid></item></channel></rss>"""
        item = parse_rss(xml, SOURCES[0].page_url)[0]
        self.assertEqual(item.url, "https://cse.pusan.ac.kr/bbs/cse/2055/42/artclView.do")

    def test_list_parser_marks_pinned_and_parses_regular_date(self):
        page = """<table><tr><td>공지</td><td><a href="/bbs/cse/2055/100/artclView.do">Pinned</a></td><td>2020.01.01</td></tr>
        <tr><td>5884</td><td><a href="/bbs/cse/2055/200/artclView.do">New</a></td><td>2026.10.06</td></tr></table>"""
        items = parse_list_page(SOURCES[0], page)
        self.assertTrue(items[0].is_pinned)
        self.assertFalse(items[1].is_pinned)
        self.assertEqual(items[1].published_at.date().isoformat(), "2026-10-06")

    def test_detail_keeps_raw_html_and_attachment_metadata(self):
        document = """<html><h2 class="artclViewTitle">장학 공지</h2><div>작성일: 2026.10.01</div>
        <div class="artclView"><p>신청자는 온라인으로 제출하세요.</p>
        <a href="/bbs/cse/2055/7/download.do">신청서.hwp</a><a href="https://forms.example/a">신청 링크</a></div></html>"""
        detail = parse_detail(SOURCES[0], "https://cse.pusan.ac.kr/bbs/cse/2055/7/artclView.do", document)
        self.assertIn("온라인으로", detail.clean_text)
        self.assertIn("<p>", detail.raw_html)
        self.assertEqual(detail.attachments[0].extension, "hwp")
        self.assertEqual(detail.external_id, "7")

    def _detail(self, body="正文一"):
        return NoticeDetail("99", "标题", "https://cse.pusan.ac.kr/bbs/cse/2055/99/artclView.do", "作者",
                            datetime(2026, 10, 1, tzinfo=SEOUL), "일반", f"<p>{body}</p>", body,
                            [Attachment("a.pdf", "https://example.test/a.pdf", "pdf")])

    @staticmethod
    def _analysis(importance="high"):
        return {"title_zh": "中文标题", "summary_zh": "这是公告摘要。", "categories": ["academic"],
                "audience": ["undergraduate"], "action_required": True, "actions": ["在线申请"],
                "deadlines": [], "eligibility": [], "required_documents": [], "importance": importance,
                "delivery_priority": "immediate", "warnings": [], "status": "active",
                "attachment_requires_review": True, "confidence": 0.9}

    def test_notice_save_is_idempotent_and_records_revision(self):
        crawler = Crawler(self.db, self.settings)
        self.assertEqual(crawler.save(SOURCES[0], self._detail(), False)[0], "new")
        self.assertEqual(crawler.save(SOURCES[0], self._detail(), False)[0], "unchanged")
        self.assertEqual(crawler.save(SOURCES[0], self._detail("正文二"), False)[0], "updated")
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notice_revisions").fetchone()[0], 1)

    def test_datetime_parser_keeps_iso_fractional_seconds(self):
        parsed = parse_datetime("2026-10-07T12:34:56.123456+09:00")
        self.assertEqual(parsed.microsecond, 123456)

    def test_image_only_notice_hash_tracks_image_source(self):
        first = self._detail("")
        first.raw_html = '<p><img src="/first.png"></p>'
        second = self._detail("")
        second.raw_html = '<p><img src="/second.png"></p>'
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_double_opt_in_is_one_time_and_unsubscribe_persists(self):
        mailer = FakeMailer()
        service = SubscriptionService(self.db, self.settings, mailer)
        service.subscribe(" Student@Example.com ", [SOURCES[0].key], True, True, True)
        token = mailer.sent[0][2].split("token=", 1)[1].split("'", 1)[0]
        management = service.verify(token)
        with self.assertRaises(ValueError):
            service.verify(token)
        service.update(management, [], False, False, False, "unsubscribe")
        subscriber, _ = service.get_by_management_token(management)
        self.assertEqual(subscriber["status"], "unsubscribed")
        self.assertIsNotNone(subscriber["unsubscribed_at"])

    def test_historical_notice_never_creates_delivery(self):
        crawler = Crawler(self.db, self.settings)
        _, notice_id = crawler.save(SOURCES[0], self._detail(), True)
        created = DeliveryPlanner(self.db, self.settings).plan_notice(notice_id, {
            "importance": "critical", "deadlines": []
        })
        self.assertEqual(created, 0)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0], 0)

    def test_new_revision_supersedes_unsent_digest(self):
        now = iso_utc()
        with self.db.transaction() as conn:
            subscriber_id = conn.execute(
                "INSERT INTO subscribers(email,email_normalized,status,created_at) VALUES('a@example.com','a@example.com','active',?)",
                (now,),
            ).lastrowid
            conn.execute("""INSERT INTO subscriptions(subscriber_id,source_key,enabled,immediate_enabled,
                            daily_digest_enabled,weekly_digest_enabled,created_at,updated_at)
                            VALUES(?,?,1,1,1,1,?,?)""", (subscriber_id, SOURCES[0].key, now, now))
        crawler = Crawler(self.db, self.settings)
        _, notice_id = crawler.save(SOURCES[0], self._detail(), False)
        planner = DeliveryPlanner(self.db, self.settings)
        planner.plan_notice(notice_id, {"importance": "normal", "deadlines": []})
        crawler.save(SOURCES[0], self._detail("更新正文"), False)
        planner.plan_notice(notice_id, {"importance": "normal", "deadlines": []})
        with self.db.connect() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM notification_deliveries WHERE notice_id=? ORDER BY id", (notice_id,))]
        self.assertEqual(statuses, ["cancelled", "pending"])

    def test_delivery_rechecks_unsubscribe_before_sending(self):
        now = iso_utc()
        with self.db.transaction() as conn:
            subscriber_id = conn.execute(
                """INSERT INTO subscribers(email,email_normalized,status,created_at,management_token_hash,
                   management_token_created_at) VALUES('a@example.com','a@example.com','active',?,'hash',?)""", (now, now)
            ).lastrowid
            conn.execute("""INSERT INTO subscriptions(subscriber_id,source_key,enabled,immediate_enabled,
                            daily_digest_enabled,weekly_digest_enabled,created_at,updated_at)
                            VALUES(?,?,1,1,1,1,?,?)""", (subscriber_id, SOURCES[0].key, now, now))
        crawler = Crawler(self.db, self.settings)
        _, notice_id = crawler.save(SOURCES[0], self._detail(), False)
        with self.db.transaction() as conn:
            content_hash = conn.execute("SELECT content_hash FROM notices WHERE id=?", (notice_id,)).fetchone()[0]
            conn.execute("INSERT INTO ai_analyses(notice_id,content_hash,result_json,model,created_at) VALUES(?,?,?,?,?)",
                         (notice_id, content_hash, json.dumps(self._analysis()), "test", now))
        DeliveryPlanner(self.db, self.settings).plan_notice(notice_id, self._analysis())
        with self.db.transaction() as conn:
            conn.execute("UPDATE subscribers SET status='unsubscribed',unsubscribed_at=? WHERE id=?", (iso_utc(), subscriber_id))
        mailer = FakeMailer()
        stats = DeliveryWorker(self.db, self.settings, mailer).process_due()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(mailer.sent, [])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM notification_deliveries").fetchone()[0], "cancelled")

    def _active_subscriber(self, immediate=1, daily=1, weekly=1, email="a@example.com"):
        now = iso_utc()
        with self.db.transaction() as conn:
            subscriber_id = conn.execute(
                """INSERT INTO subscribers(email,email_normalized,status,created_at,management_token_hash,
                   management_token_created_at) VALUES(?,?,'active',?,'hash',?)""", (email, email, now, now)
            ).lastrowid
            conn.execute("""INSERT INTO subscriptions(subscriber_id,source_key,enabled,immediate_enabled,
                            daily_digest_enabled,weekly_digest_enabled,created_at,updated_at)
                            VALUES(?,?,1,?,?,?,?,?)""", (subscriber_id, SOURCES[0].key, immediate, daily, weekly, now, now))
        return subscriber_id

    def test_notice_is_still_delivered_without_ai_key(self):
        self._active_subscriber()
        settings = replace(self.settings, openai_api_key=None)
        planner = DeliveryPlanner(self.db, settings)
        _, notice_id = Crawler(self.db, settings).save(SOURCES[0], self._detail(), False)
        stats = AIWorker(self.db, OpenAIAnalyzer(settings), planner).process_pending()
        self.assertEqual(stats["failed"], 1)
        with self.db.connect() as conn:
            kinds = [row[0] for row in conn.execute("SELECT notification_type FROM notification_deliveries WHERE notice_id=?",
                                                    (notice_id,))]
            model = conn.execute("SELECT model FROM ai_analyses WHERE notice_id=?", (notice_id,)).fetchone()[0]
        self.assertEqual(kinds, ["daily_digest"])
        self.assertEqual(model, "fallback-no-ai")

    def test_important_notice_falls_back_to_digest_when_immediate_disabled(self):
        self._active_subscriber(immediate=0, daily=1, weekly=0)
        _, notice_id = Crawler(self.db, self.settings).save(SOURCES[0], self._detail(), False)
        DeliveryPlanner(self.db, self.settings).plan_notice(notice_id, {"importance": "high", "deadlines": []})
        with self.db.connect() as conn:
            kinds = [row[0] for row in conn.execute("SELECT notification_type FROM notification_deliveries")]
        self.assertEqual(kinds, ["daily_digest"])

    def test_old_notice_first_seen_by_polling_is_historical(self):
        crawler = Crawler(self.db, self.settings)
        with self.db.transaction() as conn:
            conn.execute("UPDATE sources SET backfill_completed_at=? WHERE source_key=?",
                         (iso_utc(datetime(2026, 10, 5, tzinfo=SEOUL)), SOURCES[0].key))
        old, new = self._detail(), self._detail()
        new.external_id, new.published_at = "100", datetime(2026, 10, 6, 9, tzinfo=SEOUL)
        details = {old.url: old, "new": new}
        crawler.fetch_detail = lambda source, item: details[item.url]
        discovered = [DiscoveredNotice("99", "t", old.url), DiscoveredNotice("100", "t", "new")]
        import pnu_notice.crawler as crawler_module
        original = crawler_module.parse_rss
        crawler_module.parse_rss = lambda *args: discovered
        crawler.http.request = lambda url: type("R", (), {"text": ""})()
        try:
            crawler.run_incremental(SOURCES[0])
        finally:
            crawler_module.parse_rss = original
        with self.db.connect() as conn:
            flags = dict(conn.execute("SELECT external_notice_id,historical_import FROM notices").fetchall())
        self.assertEqual(flags, {"99": 1, "100": 0})

    def test_resubscribe_by_third_party_does_not_disturb_active_subscription(self):
        mailer = FakeMailer()
        service = SubscriptionService(self.db, replace(self.settings, verification_cooldown_seconds=0), mailer)
        service.subscribe("a@example.com", [SOURCES[0].key], True, True, True)
        management = service.verify(mailer.sent[0][2].split("token=", 1)[1].split("'", 1)[0])
        service.subscribe("a@example.com", [SOURCES[1].key], False, False, True)
        subscriber, subscriptions = service.get_by_management_token(management)
        self.assertEqual(subscriber["status"], "active")
        self.assertEqual([item["source_key"] for item in subscriptions], [SOURCES[0].key])
        self.assertEqual([subject for _, subject, _, _ in mailer.sent],
                         ["还差一步：确认你的 PNU Notice 订阅", "欢迎订阅 PNU Notice", "还差一步：确认你的 PNU Notice 订阅"])
        self.assertEqual(service.verify(mailer.sent[2][2].split("token=", 1)[1].split("'", 1)[0]), management)
        _, subscriptions = service.get_by_management_token(management)
        self.assertEqual([item["source_key"] for item in subscriptions], [SOURCES[1].key])
        self.assertEqual(len(mailer.sent), 3)  # confirming new preferences of an active subscriber sends no second welcome

    def test_welcome_email_describes_choices_and_failure_keeps_subscription(self):
        mailer = FakeMailer()
        service = SubscriptionService(self.db, self.settings, mailer)
        service.subscribe("w@example.com", [SOURCES[2].key], True, True, False)
        service.verify(mailer.sent[0][2].split("token=", 1)[1].split("'", 1)[0])
        to, subject, body, _ = mailer.sent[1]
        self.assertEqual((to, subject), ("w@example.com", "欢迎订阅 PNU Notice"))
        self.assertIn(SOURCES[2].display_name, body)
        self.assertIn("10:00、13:00、16:00、19:00", body)
        self.assertNotIn("低优先级公告", body)  # weekly digest was not chosen
        self.assertIn("/unsubscribe?token=", body)

        class BrokenAfterFirst(FakeMailer):
            def send(self, to, subject, body_html, key):
                if subject.startswith("欢迎"):
                    raise RuntimeError("provider down")
                return super().send(to, subject, body_html, key)
        broken = BrokenAfterFirst()
        service = SubscriptionService(self.db, self.settings, broken)
        service.subscribe("x@example.com", [SOURCES[0].key], True, True, True)
        management = service.verify(broken.sent[0][2].split("token=", 1)[1].split("'", 1)[0])
        subscriber, _ = service.get_by_management_token(management)
        self.assertEqual(subscriber["status"], "active")
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM notification_deliveries WHERE notification_type='welcome' "
                                          "AND subscriber_id=?", (subscriber["id"],)).fetchone()[0], "failed")

    def test_detail_text_is_not_split_by_inline_tags(self):
        document = """<div class="artclView"><p><span>2026</span>년 <b>10</b>월 신청 안내</p><p>둘째 줄</p></div>"""
        detail = parse_detail(SOURCES[0], "https://cse.pusan.ac.kr/bbs/cse/2055/7/artclView.do", document)
        self.assertEqual(detail.clean_text, "2026년 10월 신청 안내\n둘째 줄")


    def _post_subscribe(self, settings, mailer, siteverify_result):
        body = "email=a%40example.com&source=cse_undergraduate&daily=on&cf-turnstile-response=tok".encode()
        environ = {"PATH_INFO": "/subscribe", "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)),
                   "wsgi.input": io.BytesIO(body), "HTTP_CF_CONNECTING_IP": "203.0.113.5"}
        statuses = []
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(siteverify_result).encode()
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            page = b"".join(WebApp(SubscriptionService(self.db, settings, mailer))(
                environ, lambda status, headers: statuses.append(status))).decode()
        return statuses[0], page, urlopen

    def test_subscribe_requires_valid_turnstile_token(self):
        settings = replace(self.settings, turnstile_site_key="sitekey", turnstile_secret="secret",
                           turnstile_hostnames=("notice.example.test",))
        good = {"success": True, "action": "subscribe", "hostname": "notice.example.test"}
        for result in ({**good, "success": False}, {**good, "action": "other"}, {**good, "hostname": "evil.test"}):
            mailer = FakeMailer()
            status, page, _ = self._post_subscribe(settings, mailer, result)
            self.assertTrue(status.startswith("403"), result)
            self.assertEqual(mailer.sent, [])
        mailer = FakeMailer()
        status, page, urlopen = self._post_subscribe(settings, mailer, good)
        self.assertTrue(status.startswith("200"))
        self.assertEqual(len(mailer.sent), 1)
        self.assertIn(b"remoteip=203.0.113.5", urlopen.call_args.args[0].data)

    def test_subscribe_without_turnstile_config_is_not_gated(self):
        mailer = FakeMailer()
        status, _, urlopen = self._post_subscribe(self.settings, mailer, {})
        self.assertTrue(status.startswith("200"))
        urlopen.assert_not_called()


    def test_failed_verification_send_does_not_start_cooldown(self):
        class BrokenMailer(FakeMailer):
            def send(self, *args):
                raise RuntimeError("mail provider down")
        service = SubscriptionService(self.db, self.settings, BrokenMailer())
        with self.assertRaises(RuntimeError):
            service.subscribe("a@example.com", [SOURCES[0].key], True, True, True)
        mailer = FakeMailer()
        SubscriptionService(self.db, self.settings, mailer).subscribe("a@example.com", [SOURCES[0].key], True, True, True)
        self.assertEqual(len(mailer.sent), 1)
        with self.assertRaises(ValueError):
            SubscriptionService(self.db, self.settings, mailer).subscribe("a@example.com", [SOURCES[0].key], True, True, True)


    def test_replanning_same_content_keeps_pending_digest_and_never_resends(self):
        self._active_subscriber()
        _, notice_id = Crawler(self.db, self.settings).save(SOURCES[0], self._detail(), False)
        planner = DeliveryPlanner(self.db, self.settings)
        planner.plan_notice(notice_id, {"importance": "normal", "deadlines": []})
        planner.plan_notice(notice_id, {"importance": "normal", "deadlines": []})
        with self.db.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT status FROM notification_deliveries")], ["pending"])
            conn.execute("UPDATE notification_deliveries SET status='sent'")
            conn.commit()
        planner.plan_notice(notice_id, {"importance": "normal", "deadlines": []})
        with self.db.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT status FROM notification_deliveries")], ["sent"])


    def test_exhausted_ai_quota_pauses_queue_without_falling_back(self):
        self._active_subscriber()
        settings = replace(self.settings, openai_api_key="key")
        crawler = Crawler(self.db, settings)
        crawler.save(SOURCES[0], self._detail(), False)
        second = self._detail()
        second.external_id = "100"
        crawler.save(SOURCES[0], second, False)

        class BrokeAnalyzer(OpenAIAnalyzer):
            calls = 0

            def analyze(self, notice, attachments):
                BrokeAnalyzer.calls += 1
                raise RuntimeError('HTTP 429: {"type": "insufficient_quota"}')

        stats = AIWorker(self.db, BrokeAnalyzer(settings), DeliveryPlanner(self.db, settings)).process_pending()
        self.assertEqual(BrokeAnalyzer.calls, 2)  # one notice, two tries, then the whole queue pauses
        self.assertEqual(stats.get("paused_for_quota"), 1)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT ai_status,ai_attempts,ai_next_attempt_at FROM notices").fetchall()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0], 0)
        self.assertTrue(all(r[0] == "retrying" and r[1] == 0 and r[2] for r in rows))


    def test_digest_slots_are_four_times_on_weekdays_only(self):
        hours = (10, 13, 16, 19)
        def slot(*args):
            return next_digest_slot(datetime(*args, tzinfo=SEOUL), hours).strftime("%a %H:%M")
        self.assertEqual(slot(2026, 10, 7, 8, 0), "Wed 10:00")    # before the first slot
        self.assertEqual(slot(2026, 10, 7, 10, 0), "Wed 13:00")   # exactly on a slot moves to the next one
        self.assertEqual(slot(2026, 10, 7, 14, 30), "Wed 16:00")  # at most 3 hours of waiting during the day
        self.assertEqual(slot(2026, 10, 7, 19, 5), "Thu 10:00")   # evening rolls to the next morning
        self.assertEqual(slot(2026, 10, 9, 19, 30), "Mon 10:00")  # Friday evening skips the weekend
        self.assertEqual(slot(2026, 10, 10, 12, 0), "Mon 10:00")  # Saturday
        self.assertEqual(slot(2026, 10, 11, 23, 0), "Mon 10:00")  # Sunday night


    def test_unparseable_deadline_keeps_analysis_and_original_text(self):
        from pnu_notice.ai import validate_analysis
        result = validate_analysis({**self._analysis(), "deadlines": [
            {"type": "申请截止", "datetime": "10月中旬", "timezone": "Asia/Seoul", "original_text": "10월 중순", "confidence": 0.4}]})
        self.assertIsNone(result["deadlines"][0]["datetime"])
        self.assertEqual(result["deadlines"][0]["original_text"], "10월 중순")


    def test_notice_email_shows_seoul_time_and_readable_audience(self):
        from pnu_notice.emailer import notice_email
        subscriber = {"id": 1, "email": "a@example.com", "management_token_created_at": iso_utc()}
        notice = {"original_title": "t", "original_url": "https://example.test", "source_key": SOURCES[0].key,
                  "source_name": "x", "published_at": "2026-10-07T07:18:50.950000+00:00"}
        analysis = {**self._analysis(), "audience": ["undergraduate", "graduating_student"]}
        _, body = notice_email(self.settings, subscriber, notice, analysis, "new_notice")
        self.assertIn("2026-10-07 16:18", body)
        self.assertIn("本科生、应届毕业生", body)
        self.assertNotIn("graduating_student", body)


    def test_only_student_action_deadlines_are_reminded(self):
        for kind in ("报名截止", "申请截止", "材料提交截止", "选课取消（W）申请截止", "缴费期限", "Application deadline"):
            self.assertTrue(is_student_deadline(kind, 0.9), kind)
        for kind in ("活动时间", "考试（第一场）", "期中考试结束", "预计成绩公布", "申请期间开始",
                     "学院向教育创新室提交申请审查请求的截止日期（非学生申请期限）"):
            self.assertFalse(is_student_deadline(kind, 0.9), kind)
        self.assertFalse(is_student_deadline("报名截止", 0.4))  # the AI itself was unsure

    def test_reminder_times_follow_the_morning_slot(self):
        hours = (10, 13, 16, 19)
        def at(deadline, days):
            return reminder_time(datetime(*deadline, tzinfo=SEOUL), days, hours).strftime("%m-%d %H:%M")
        self.assertEqual(at((2026, 10, 16, 15, 0), 7), "10-09 10:00")
        self.assertEqual(at((2026, 10, 16, 15, 0), 1), "10-15 10:00")
        self.assertEqual(at((2026, 10, 16, 15, 0), 0), "10-16 10:00")
        self.assertEqual(at((2026, 10, 16, 11, 0), 0), "10-15 19:00")  # 10:00 would be under 2 hours before
        self.assertEqual(at((2026, 10, 18, 23, 59), 0), "10-18 10:00")  # weekends too
        self.assertEqual(at((2026, 10, 8, 0, 0), 0), "10-08 10:00")  # date-only deadline means end of that day
        self.assertEqual(at((2026, 10, 8, 0, 0), 1), "10-07 10:00")

    def _deadline_notice(self, created_at):
        _, notice_id = Crawler(self.db, self.settings).save(SOURCES[0], self._detail(), False)
        with self.db.transaction() as conn:
            content_hash = conn.execute("SELECT content_hash FROM notices WHERE id=?", (notice_id,)).fetchone()[0]
            conn.execute("UPDATE notices SET created_at=?,ai_status='completed' WHERE id=?", (iso_utc(created_at), notice_id))
            conn.execute("INSERT INTO ai_analyses(notice_id,content_hash,result_json,model,created_at) VALUES(?,?,?,?,?)",
                         (notice_id, content_hash, json.dumps(self._analysis()), "t", iso_utc()))
            for kind, when in (("报名截止", datetime(2026, 10, 16, 15, 0, tzinfo=SEOUL)),
                               ("活动时间", datetime(2026, 10, 16, 18, 0, tzinfo=SEOUL))):
                conn.execute("""INSERT INTO deadlines(notice_id,kind,deadline_at,timezone,original_text,confidence)
                                VALUES(?,?,?,'Asia/Seoul','10/16(금) 15:00까지',0.95)""", (notice_id, kind, iso_utc(when)))
        return notice_id

    def test_reminders_reach_current_subscribers_only(self):
        early = self._active_subscriber()  # subscribed before the notice was published
        late = self._active_subscriber(email="b@example.com")
        with self.db.transaction() as conn:
            conn.execute("UPDATE subscribers SET verified_at=? WHERE id=?",
                         (iso_utc(datetime(2026, 10, 9, 12, tzinfo=SEOUL)), late))
        self._deadline_notice(datetime(2026, 10, 8, 9, tzinfo=SEOUL))
        planner = ReminderPlanner(self.db, self.settings)

        def run(*when):
            planner.run(datetime(*when, tzinfo=SEOUL))
            with self.db.connect() as conn:
                return sorted(tuple(row) for row in conn.execute(
                    "SELECT subscriber_id,notification_type FROM notification_deliveries WHERE notification_type LIKE 'deadline%'"))

        self.assertEqual(run(2026, 10, 9, 10, 5), [(early, "deadline_d7")])  # late subscriber skips D-7/D-3
        self.assertEqual(run(2026, 10, 9, 13, 0), [(early, "deadline_d7")])  # same round again: no duplicates
        self.assertEqual(run(2026, 10, 13, 13, 0), [(early, "deadline_d7")])  # D-3 window missed (downtime): no catch-up
        self.assertEqual(run(2026, 10, 15, 10, 5), [(early, "deadline_d1"), (early, "deadline_d7"), (late, "deadline_d1")])
        with self.db.transaction() as conn:
            conn.execute("UPDATE subscribers SET status='unsubscribed' WHERE id=?", (late,))
        self.assertEqual(run(2026, 10, 16, 10, 5)[-2:], [(early, "deadline_day"), (late, "deadline_d1")])
        with self.db.connect() as conn:
            kinds = {row[0] for row in conn.execute("SELECT DISTINCT payload_json FROM notification_deliveries")}
        self.assertTrue(all('"deadline_id": 1' in kind for kind in kinds))  # the 活动时间 entry never triggers

    def test_due_reminders_become_one_email_and_old_scheduled_ones_are_cancelled(self):
        subscriber = self._active_subscriber()
        notice_id = self._deadline_notice(datetime(2026, 10, 1, tzinfo=SEOUL))
        now = iso_utc()
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO notification_deliveries(subscriber_id,notice_id,notification_type,dedupe_key,scheduled_at,
                            status,payload_json,created_at) VALUES(?,?,'deadline_d3','deadline_d3:1:oldhash',?,'pending','{}',?)""",
                         (subscriber, notice_id, now, now))
            second = conn.execute("""INSERT INTO deadlines(notice_id,kind,deadline_at,timezone,original_text,confidence)
                                     VALUES(?,'申请截止',?,'Asia/Seoul','10/16 17:00',0.9)""",
                                  (notice_id, iso_utc(datetime(2026, 10, 16, 17, tzinfo=SEOUL)))).lastrowid
            for deadline_id in (1, second):
                conn.execute("""INSERT INTO notification_deliveries(subscriber_id,notice_id,notification_type,dedupe_key,
                                scheduled_at,status,payload_json,created_at) VALUES(?,?,'deadline_d1',?,?,'pending',?,?)""",
                             (subscriber, notice_id, f"deadline:{deadline_id}:1", now, json.dumps({"deadline_id": deadline_id}), now))
        ReminderPlanner(self.db, self.settings).run(datetime(2026, 10, 7, 8, tzinfo=SEOUL))
        mailer = FakeMailer()
        DeliveryWorker(self.db, self.settings, mailer).process_due()
        self.assertEqual(len(mailer.sent), 1)
        _, subject, body, _ = mailer.sent[0]
        self.assertIn("[截止提醒] 2 项即将截止", subject)
        self.assertIn("报名截止", body)
        self.assertIn("10月16日 15:00", body)
        self.assertIn("/unsubscribe?token=", body)
        with self.db.connect() as conn:
            old = conn.execute("SELECT status,error FROM notification_deliveries WHERE dedupe_key='deadline_d3:1:oldhash'").fetchone()
        self.assertEqual(tuple(old), ("cancelled", "replaced by scheduled reminders"))


    def test_stored_notices_are_never_fetched_again(self):
        from datetime import timedelta
        import pnu_notice.crawler as crawler_module
        crawler = Crawler(self.db, self.settings)
        discovered = []
        for number in range(15):
            detail = self._detail(f"正文{number}")
            detail.external_id = str(1000 + number)
            crawler.save(SOURCES[0], detail, True)
            discovered.append(DiscoveredNotice(detail.external_id, "t", f"https://example.test/{number}"))
        with self.db.transaction() as conn:
            conn.execute("UPDATE notices SET last_checked_at=?", (iso_utc(datetime.now(SEOUL) - timedelta(days=2)),))
            conn.execute("UPDATE sources SET backfill_completed_at=?", (iso_utc(),))
        fetched = []
        crawler.fetch_detail = lambda source, item: fetched.append(item.url) or self._detail()
        original = crawler_module.parse_rss
        crawler_module.parse_rss = lambda *args: discovered
        crawler.http.request = lambda url: type("R", (), {"text": ""})()
        try:
            crawler.run_incremental(SOURCES[0])
        finally:
            crawler_module.parse_rss = original
        self.assertEqual(fetched, [])


    def test_empty_rss_response_is_retried_before_failing(self):
        crawler = Crawler(self.db, self.settings)
        crawler.RSS_RETRY_DELAY = 0
        feed = """<rss><channel><item><title>공지</title><link>/bbs/cse/2055/42/artclView.do</link></item></channel></rss>"""
        responses = iter(["", feed])
        crawler.http.request = lambda url: type("R", (), {"text": next(responses)})()
        self.assertEqual([item.external_id for item in crawler.fetch_rss(SOURCES[0])], ["42"])
        crawler.http.request = lambda url: type("R", (), {"text": ""})()
        with self.assertRaises(Exception):
            crawler.fetch_rss(SOURCES[0])

    def test_alerts_only_for_failures_concentrated_in_recent_hours(self):
        from datetime import timedelta
        from pnu_notice.monitoring import AlertWorker
        settings = replace(self.settings, admin_email="admin@example.com")
        mailer = FakeMailer()
        def fail(hours_ago):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                             ("crawler", SOURCES[0].key, "no element found",
                              iso_utc(datetime.now(SEOUL) - timedelta(hours=hours_ago))))
        for hours in (9, 6, 1):  # scattered over the day: no alert
            fail(hours)
        self.assertEqual(AlertWorker(self.db, settings, mailer).alert_repeated_failures(), 0)
        fail(0.5)
        fail(0.1)  # three within the last three hours: alert
        self.assertEqual(AlertWorker(self.db, settings, mailer).alert_repeated_failures(), 1)
        self.assertIn("抓取持续失败", mailer.sent[0][1])
        self.assertIn(SOURCES[0].display_name, mailer.sent[0][1])


if __name__ == "__main__":
    unittest.main()
