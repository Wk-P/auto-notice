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


AUDIENCE_LABELS = {"undergraduate": "本科生", "master": "硕士生", "phd": "博士生", "graduate": "研究生",
                   "international_student": "留学生", "prospective_student": "新生 / 准入学生",
                   "graduating_student": "应届毕业生", "chinese_student": "中国留学生",
                   "specific_nationality": "特定国籍学生", "all_students": "全体学生"}


def _seoul_time(value: str | None) -> str:
    if not value:
        return "未明确"
    return datetime.fromisoformat(value).astimezone(SEOUL).strftime("%Y-%m-%d %H:%M")


def _time_left(deadline_at: str) -> tuple[str, str]:
    """('今天截止', '10月8日 15:00')-style wording, from the real date difference in Seoul."""
    local = datetime.fromisoformat(deadline_at).astimezone(SEOUL)
    days = (local.date() - datetime.now(SEOUL).date()).days
    date_only = (local.hour, local.minute, local.second) == (0, 0, 0)  # "until that date": no misleading 00:00
    when = f"{local.month}月{local.day}日" + ("" if date_only else f" {local:%H:%M}")
    label = "今天截止" if days <= 0 else "明天截止" if days == 1 else f"还剩 {days} 天"
    return label, when


def reminder_email(settings: Settings, subscriber: dict, entries: list[tuple]) -> tuple[str, str]:
    """One email for every reminder due to this subscriber in the current round; entries are
    (notice, analysis, deadline) tuples, soonest deadline first."""
    entries = sorted(entries, key=lambda entry: entry[2]["deadline_at"])
    cards = []
    for notice, analysis, deadline in entries:
        label, when = _time_left(deadline["deadline_at"])
        source = SOURCE_BY_KEY.get(notice["source_key"])
        source_name = source.display_name if source else notice["source_name"]
        actions = "".join(f"<li>{html.escape(item)}</li>" for item in analysis.get("actions", [])[:3])
        cards.append(f"""<section style="border:1px solid #f2d4a8;background:#fffaf2;border-radius:12px;padding:16px 18px;margin:14px 0">
        <p style="margin:0 0 6px"><span style="background:#b54708;color:#fff;border-radius:999px;padding:2px 10px;font-size:13px;font-weight:bold">{label}</span>
        <strong style="margin-left:8px">{html.escape(deadline["kind"])}</strong>：{html.escape(when)}</p>
        <h2 style="font-size:17px;margin:10px 0 6px"><a href="{html.escape(notice['original_url'])}">{html.escape(analysis.get('title_zh') or notice['original_title'])}</a></h2>
        <p style="margin:0;color:#667085;font-size:14px">来源：{html.escape(source_name)} · 原文：{html.escape(deadline["original_text"])}</p>
        {f'<ul style="margin:8px 0 0;padding-left:20px">{actions}</ul>' if actions else ""}
        <p style="margin:10px 0 0"><a href="{html.escape(notice['original_url'])}">查看学校原公告</a></p></section>""")
    first_label, _ = _time_left(entries[0][2]["deadline_at"])
    first_title = entries[0][1].get("title_zh") or entries[0][0]["original_title"]
    subject = (f"[截止提醒] {first_label}：{first_title}" if len(entries) == 1
               else f"[截止提醒] {len(entries)} 项即将截止，最近的{first_label}")
    body = (f"<main style='font-family:Arial,sans-serif;max-width:680px;margin:auto;line-height:1.6'>"
            f"<p style='color:#174b9b;font-weight:bold'>PNU Notice · 截止提醒</p>"
            f"<h1 style='font-size:22px'>{'这项' if len(entries) == 1 else f'这 {len(entries)} 项'}申请 / 提交快到截止时间了</h1>"
            + "".join(cards) + footer(settings, subscriber) + "</main>")
    return subject, body


_ACCOUNT_MAIL_STYLE = "font-family:Arial,sans-serif;max-width:600px;margin:auto;line-height:1.7;color:#17213b"


def status_change_email(settings: Settings, subscriber: dict, status: str, by_admin: bool) -> tuple[str, str]:
    """Confirms a pause, resume or unsubscribe, saying who made the change and how to undo it."""
    who = "管理员已" if by_admin else "你已"
    subscribe_url = f"{settings.app_base_url}/subscribe"
    texts = {
        "paused": ("订阅已暂停", f"{who}暂停 PNU Notice 订阅。暂停期间不会收到任何公告和截止提醒邮件，订阅设置会保留。",
                   "想恢复时，点击下方“管理订阅”或登录网站，选择“重新启用订阅”。"),
        "active": ("订阅已恢复", f"{who}恢复 PNU Notice 订阅，之后会继续按你的设置接收公告和截止提醒。",
                   "暂停期间发布的公告不会补发。"),
        "unsubscribed": ("已退订", f"{who}为你退订 PNU Notice。从现在起不会再收到公告和截止提醒邮件，这是最后一封确认邮件。"
                         if by_admin else "你已退订 PNU Notice。从现在起不会再收到公告和截止提醒邮件，这是最后一封确认邮件。",
                         f"如果是误操作，可以点击下方“管理订阅”重新启用，或在 {subscribe_url} 重新订阅。"),
    }
    title, line, hint = texts[status]
    notice = "<p style='color:#667085;font-size:14px'>如果这不是你本人的操作，请检查账户安全并修改密码。</p>" if not by_admin else ""
    body = (f"<main style='{_ACCOUNT_MAIL_STYLE}'><p style='color:#174b9b;font-weight:bold'>PNU Notice</p>"
            f"<h1 style='font-size:22px'>{title}</h1><p>{html.escape(line)}</p><p>{html.escape(hint)}</p>{notice}"
            f"{footer(settings, subscriber)}</main>")
    return f"PNU Notice：{title}", body


def account_deleted_email(settings: Settings, email: str, by_admin: bool) -> tuple[str, str]:
    """Sent after the data is gone, so it carries no management links; only a way to subscribe again."""
    title = "你的账户已被管理员删除" if by_admin else "你的账户已注销"
    line = ("管理员已删除你在 PNU Notice 的账户，你的邮箱、订阅设置、密码和发信记录已被永久删除。" if by_admin
            else "你的 PNU Notice 账户已经注销，邮箱、订阅设置、密码和发信记录已被永久删除。")
    body = (f"<main style='{_ACCOUNT_MAIL_STYLE}'><p style='color:#174b9b;font-weight:bold'>PNU Notice</p>"
            f"<h1 style='font-size:22px'>{title}</h1><p>{html.escape(line)}</p>"
            f"<p>从现在起不会再收到任何邮件，这是最后一封。以后如果还想接收公告，可以随时"
            f"<a href='{html.escape(settings.app_base_url)}/subscribe'>重新订阅</a>。</p>"
            + ("" if by_admin else "<p style='color:#667085;font-size:14px'>如果这不是你本人的操作，请重新订阅并设置一个新密码。</p>")
            + "</main>")
    return f"PNU Notice：{title}", body


def welcome_email(settings: Settings, subscriber: dict, subscriptions: list[dict]) -> tuple[str, str]:
    enabled = [SOURCE_BY_KEY[item["source_key"]].display_name for item in subscriptions
               if item["enabled"] and item["source_key"] in SOURCE_BY_KEY]
    immediate = any(item["immediate_enabled"] for item in subscriptions)
    daily = any(item["daily_digest_enabled"] for item in subscriptions)
    weekly = any(item["weekly_digest_enabled"] for item in subscriptions)
    hours = "、".join(f"{hour}:00" for hour in settings.digest_hours)
    weekday = "一二三四五六日"[settings.weekly_digest_weekday]
    plan = []
    if immediate:
        plan.append(("重要公告", "签证、奖学金、毕业等重要事项，分析完成后立即发送。"))
        plan.append(("截止提醒", f"申请、报名、提交等截止日期前 7 天、3 天、1 天和当天上午 {min(settings.digest_hours)}:00 提醒"
                                "（订阅之前已发布的公告只提醒前 1 天和当天）。"))
    if daily:
        plan.append(("普通公告", f"工作日 {hours}（首尔时间）合并成一封汇总，发布后通常 3 小时内收到。"))
    if weekly:
        plan.append(("低优先级公告", f"讲座、活动、宣传等，每周{weekday} {settings.weekly_digest_hour}:00 汇总一次。"))
    rows = "".join(f"<tr><td style='padding:10px 12px;font-weight:bold;white-space:nowrap;vertical-align:top'>{html.escape(name)}</td>"
                   f"<td style='padding:10px 12px;color:#475467'>{html.escape(text)}</td></tr>" for name, text in plan)
    account = f"{settings.app_base_url}/account"
    body = f"""<main style="font-family:Arial,sans-serif;max-width:600px;margin:auto;line-height:1.7;color:#17213b">
    <p style="color:#174b9b;font-weight:bold">PNU Notice</p>
    <h1 style="font-size:24px;margin:0 0 12px">欢迎订阅，订阅已经生效</h1>
    <p>从现在起，以下来源的新公告会自动整理成中文摘要，发送到 <strong>{html.escape(subscriber['email'])}</strong>：</p>
    <p style="background:#f2f6fc;border-radius:10px;padding:12px 16px;font-weight:bold">{html.escape("、".join(enabled) or "（暂未选择来源）")}</p>
    <h2 style="font-size:17px;margin:24px 0 6px">你会在什么时候收到邮件</h2>
    <table style="border-collapse:collapse;width:100%;background:#f9fafb;border-radius:10px">{rows}</table>
    <h2 style="font-size:17px;margin:24px 0 6px">两个小建议</h2>
    <ul style="padding-left:20px;color:#475467">
    <li>把 <strong>{html.escape(settings.from_email)}</strong> 加入通讯录，避免公告邮件被当成垃圾邮件。</li>
    <li>设置一个密码，以后可以直接登录修改订阅：<a href="{html.escape(account)}">打开我的订阅</a></li></ul>
    <p style="color:#667085;font-size:14px">历史公告不会补发，你只会收到从现在开始发布的新公告。</p>
    {footer(settings, subscriber)}</main>"""
    return "欢迎订阅 PNU Notice", body


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
    audience = "、".join(AUDIENCE_LABELS.get(item, item) for item in analysis["audience"]) or "以原公告为准"
    body = f"""<main style="font-family:Arial,sans-serif;max-width:680px;margin:auto;line-height:1.65">
    <p style="color:#0b5cab;font-weight:bold">PNU Notice · 重要度：{importance}</p>
    <h1 style="font-size:24px">{html.escape(analysis['title_zh'] or notice['original_title'])}</h1>
    <p><strong>来源：</strong>{html.escape(source_name)}<br>
    <strong>发布时间：</strong>{html.escape(_seoul_time(notice['published_at']))}<br>
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
