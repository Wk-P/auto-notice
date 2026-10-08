from __future__ import annotations

import html
from datetime import datetime, timedelta

from .admin import COMPONENTS, _explain
from .config import Settings
from .db import Database
from .emailer import ResendMailer
from .sources import SOURCE_BY_KEY
from .timeutil import SEOUL, iso_utc, now_utc

ALERT_WINDOW = timedelta(hours=3)
ALERT_THRESHOLD = 3


class AlertWorker:
    def __init__(self, db: Database, settings: Settings, mailer: ResendMailer):
        self.db = db
        self.settings = settings
        self.mailer = mailer

    def alert_repeated_failures(self) -> int:
        if not self.settings.admin_email:
            return 0
        # Only failures from the last few hours count, so isolated hiccups spread over a day never add up to an
        # alert; with hourly crawling, three in this window means every recent run failed.
        since = iso_utc(now_utc() - ALERT_WINDOW)
        with self.db.connect() as conn:
            groups = conn.execute(
                """SELECT component,COALESCE(source_key,'') AS source_key,COUNT(*) AS failures,MAX(occurred_at) AS latest,
                   (SELECT error_message FROM job_failures j2 WHERE j2.component=j.component
                    AND COALESCE(j2.source_key,'')=COALESCE(j.source_key,'') ORDER BY id DESC LIMIT 1) AS message
                   FROM job_failures j WHERE alerted_at IS NULL AND occurred_at>?
                   GROUP BY component,COALESCE(source_key,'') HAVING COUNT(*)>=?""", (since, ALERT_THRESHOLD)
            ).fetchall()
        sent = 0
        for group in groups:
            source = SOURCE_BY_KEY[group["source_key"]].display_name if group["source_key"] in SOURCE_BY_KEY else "全局"
            label = COMPONENTS.get(group["component"], group["component"])
            latest = datetime.fromisoformat(group["latest"]).astimezone(SEOUL).strftime("%m-%d %H:%M")
            body = (f"<main style='font-family:Arial,sans-serif;line-height:1.7'><h1 style='font-size:20px'>PNU Notice：{html.escape(label)}持续失败</h1>"
                    f"<p>来源：{html.escape(source)}<br>最近 {ALERT_WINDOW.seconds // 3600} 小时内失败 {group['failures']} 次，"
                    f"最近一次在首尔时间 {latest}。</p><p>最近一次的错误：<br><code>{html.escape(_explain(group['message']))}</code></p>"
                    f"<p><a href='{html.escape(self.settings.app_base_url)}/admin'>打开管理后台查看</a></p></main>")
            key = f"admin-alert-{group['component']}-{group['source_key']}-{group['latest']}"
            self.mailer.send(self.settings.admin_email, f"[PNU Notice 告警] {label}持续失败（{source}）", body, key[:250])
            with self.db.transaction() as conn:
                conn.execute("UPDATE job_failures SET alerted_at=? WHERE alerted_at IS NULL AND component=? AND COALESCE(source_key,'')=?",
                             (iso_utc(), group["component"], group["source_key"]))
            sent += 1
        return sent
