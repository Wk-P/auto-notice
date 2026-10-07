from __future__ import annotations

import html
import json
from datetime import datetime
from urllib.parse import quote

from .config import Settings
from .http import HttpClient
from .security import management_token
from .sources import SOURCE_BY_KEY
from .timeutil import SEOUL


DISCLAIMER = "以上内容由 AI 根据釜山大学官方公告自动整理，仅用于辅助快速理解。具体要求请以学校原公告及附件为准。"


class ResendMailer:
    def __init__(self, settings: Settings, http: HttpClient | None = None):
        self.settings = settings
        self.http = http or HttpClient(settings.http_timeout_seconds, 10)

    def send(self, to: str, subject: str, body_html: str, idempotency_key: str) -> str:
        if not self.settings.resend_api_key:
            raise RuntimeError("RESEND_API_KEY is not configured")
        payload = {"from": f"{self.settings.from_name} <{self.settings.from_email}>", "to": [to],
                   "subject": subject, "html": body_html}
        response = self.http.request(
            "https://api.resend.com/emails", method="POST", json_body=payload,
            headers={"Authorization": f"Bearer {self.settings.resend_api_key}",
                     "Idempotency-Key": idempotency_key}, retries=2,
        )
        value = json.loads(response.text)
        if not value.get("id"):
            raise RuntimeError(f"Resend response missing message id: {response.text[:500]}")
        return value["id"]


def _list(items: list[str], empty: str = "无") -> str:
    if not items:
        return f"<p>{empty}</p>"
    return "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ul>"


def footer(settings: Settings, subscriber: dict) -> str:
    token = management_token(subscriber["id"], subscriber["management_token_created_at"], settings.app_secret)
    manage = f"{settings.app_base_url}/subscription/manage?token={quote(token)}"
    unsubscribe = f"{settings.app_base_url}/unsubscribe?token={quote(token)}"
    return (f"<hr><p style='color:#666'>{DISCLAIMER}</p>"
            f"<p><a href='{html.escape(manage)}'>管理订阅</a> · "
            f"<a href='{html.escape(unsubscribe)}'>退订</a></p>")


def notice_email(settings: Settings, subscriber: dict, notice: dict, analysis: dict,
                 notification_type: str) -> tuple[str, str]:
    updated = notification_type == "updated_notice"
    prefix = "[公告更新]" if updated else "[重要公告]"
    importance = {"critical": "紧急", "high": "高", "normal": "普通", "low": "低"}[analysis["importance"]]
    deadlines = [item["original_text"] for item in analysis["deadlines"]]
    warning = list(analysis["warnings"])
    if analysis["attachment_requires_review"]:
        warning.append("该公告包含附件，自动摘要可能未覆盖附件内全部要求，请查看学校原附件。")
    subject = f"{prefix} {analysis['title_zh'] or notice['original_title']}"
    source_name = SOURCE_BY_KEY.get(notice["source_key"])
    source_name = source_name.display_name if source_name else notice["source_name"]
    audience = "、".join(analysis["audience"]) or "以原公告为准"
    body = f"""<main style="font-family:Arial,sans-serif;max-width:680px;margin:auto;line-height:1.65">
    <p style="color:#0b5cab;font-weight:bold">PNU Notice · 重要度：{importance}</p>
    <h1 style="font-size:24px">{html.escape(analysis['title_zh'] or notice['original_title'])}</h1>
    <p><strong>来源：</strong>{html.escape(source_name)}<br>
    <strong>发布时间：</strong>{html.escape(notice['published_at'] or '未明确')}<br>
    <strong>截止：</strong>{html.escape('；'.join(deadlines) if deadlines else '未明确')}<br>
    <strong>适合人群：</strong>{html.escape(audience)}</p>
    <h2>中文摘要</h2><p>{html.escape(analysis['summary_zh'])}</p>
    <h2>你需要做</h2>{_list(analysis['actions'], '无需操作')}
    <h2>需要准备</h2>{_list(analysis['required_documents'])}
    <h2>注意</h2>{_list(warning)}
    <p><a href="{html.escape(notice['original_url'])}">查看学校原公告</a></p>
    {footer(settings, subscriber)}</main>"""
    return subject, body


def digest_email(settings: Settings, subscriber: dict, entries: list[tuple[dict, dict]], weekly: bool) -> tuple[str, str]:
    local = datetime.now(SEOUL)
    title = "本周 PNU 公告汇总" if weekly else f"PNU 公告汇总 · {local.month}月{local.day}日 {local.hour}:00"
    cards = []
    for notice, analysis in entries:
        deadline = "；".join(item["original_text"] for item in analysis["deadlines"]) or "未明确"
        source = SOURCE_BY_KEY.get(notice["source_key"])
        source_name = source.display_name if source else notice["source_name"]
        cards.append(f"""<section style="padding:14px 0;border-bottom:1px solid #ddd">
        <h2 style="font-size:18px"><a href="{html.escape(notice['original_url'])}">{html.escape(analysis['title_zh'])}</a></h2>
        <p><strong>来源：</strong>{html.escape(source_name)} · <strong>需要行动：</strong>{'是' if analysis['action_required'] else '否'}<br>
        <strong>截止：</strong>{html.escape(deadline)}</p><p>{html.escape(analysis['summary_zh'])}</p></section>""")
    body = (f"<main style='font-family:Arial,sans-serif;max-width:680px;margin:auto;line-height:1.6'>"
            f"<h1>{title}</h1><p>共有 {len(entries)} 条与你订阅内容相关的公告。</p>"
            + "".join(cards) + footer(settings, subscriber) + "</main>")
    return title, body
