from __future__ import annotations

import html

from .config import Settings
from .db import Database
from .emailer import ResendMailer
from .timeutil import iso_utc


class AlertWorker:
    def __init__(self, db: Database, settings: Settings, mailer: ResendMailer):
        self.db = db
        self.settings = settings
        self.mailer = mailer

    def alert_repeated_failures(self) -> int:
        if not self.settings.admin_email:
            return 0
        with self.db.connect() as conn:
            groups = conn.execute(
                """SELECT component,COALESCE(source_key,'') AS source_key,COUNT(*) AS failures,MAX(occurred_at) AS latest
                   FROM job_failures WHERE alerted_at IS NULL GROUP BY component,COALESCE(source_key,'') HAVING COUNT(*)>=3"""
            ).fetchall()
        sent = 0
        for group in groups:
            source = group["source_key"] or "全局"
            body = ("<main><h1>PNU Notice 连续失败告警</h1>"
                    f"<p>组件：{html.escape(group['component'])}</p><p>来源：{html.escape(source)}</p>"
                    f"<p>未确认失败次数：{group['failures']}</p><p>最近发生：{html.escape(group['latest'])}</p></main>")
            key = f"admin-alert-{group['component']}-{source}-{group['latest']}"
            self.mailer.send(self.settings.admin_email, f"[PNU Notice 告警] {group['component']} 连续失败", body, key[:250])
            with self.db.transaction() as conn:
                conn.execute("UPDATE job_failures SET alerted_at=? WHERE alerted_at IS NULL AND component=? AND COALESCE(source_key,'')=?",
                             (iso_utc(), group["component"], group["source_key"]))
            sent += 1
        return sent
