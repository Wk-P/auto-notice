from __future__ import annotations

import html
import json
import math
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from .config import Settings
from .db import Database
from .delivery import next_digest_slot
from .sources import SOURCE_BY_KEY, SOURCES
from .timeutil import SEOUL, iso_utc, now_utc

PAGE_SIZE = 50
STATUS_LABELS = {"active": "接收中", "paused": "已暂停", "unsubscribed": "已退订", "pending": "待验证",
                 "email_invalid": "邮箱无效"}
STATUS_TONES = {"active": "green", "pending": "amber", "paused": "gray", "unsubscribed": "gray", "email_invalid": "red"}
SOURCE_SHORT = {"cse_undergraduate": ("计算机本科", "blue"), "cse_graduate": ("计算机大学院", "purple"),
                "international_student": ("国际处", "teal")}
IMPORTANCE = {"critical": ("紧急", "red"), "high": ("重要", "amber"), "normal": ("普通", "blue"), "low": ("低", "gray")}
AI_STATES = {"completed": ("已分析", "green"), "failed": ("原文降级", "gray"), "pending": ("排队中", "amber"),
             "retrying": ("重试中", "amber")}
NOTICE_STATUS = {"active": "进行中", "upcoming": "即将开始", "expired": "已过期", "closed": "已结束", "informational": "通知"}
DELIVERY_TYPES = {"new_notice": "即时：新公告", "updated_notice": "即时：公告更新", "daily_digest": "定时汇总",
                  "weekly_digest": "每周汇总", "deadline_d7": "截止提醒 D-7", "deadline_d3": "截止提醒 D-3",
                  "deadline_d1": "截止提醒 D-1", "deadline_day": "截止提醒 当天", "verification": "验证邮件"}
DELIVERY_STATUS = {"pending": "待发送", "sending": "发送中", "sent": "已发送", "retrying": "重试中", "failed": "失败",
                   "cancelled": "已取消"}
COMPONENTS = {"crawler": "抓取", "crawler_detail": "抓取详情页", "ai": "AI 分析", "ai_quota": "AI 余额不足",
              "delivery_planner": "发信规划"}

# Served as a file because the CSP forbids inline scripts: asks before destructive admin actions.
ADMIN_JS = """document.addEventListener('submit', function (event) {
  var button = event.submitter;
  if (button && button.dataset.confirm && !window.confirm(button.dataset.confirm)) event.preventDefault();
});
"""

ADMIN_STYLE = """
:root{--bg:#f4f6fa;--panel:#fff;--line:#e4e8f0;--text:#101828;--muted:#667085;--brand:#174b9b;--side:#0f2a52}
*{box-sizing:border-box}body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}.shell{display:grid;grid-template-columns:216px minmax(0,1fr);min-height:100vh}
.side{background:var(--side);color:#c7d3ea;padding:20px 12px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
.logo{color:#fff;font-weight:800;font-size:18px;padding:2px 12px 20px;text-decoration:none;display:block}.logo small{display:block;font-weight:500;font-size:12px;color:#8fa5c9}
.side nav a{display:block;color:#c7d3ea;text-decoration:none;padding:9px 12px;border-radius:8px;margin-bottom:2px}.side nav a:hover{background:#ffffff14;color:#fff}.side nav a.on{background:#ffffff24;color:#fff;font-weight:600}
.foot{margin-top:auto;padding:10px 12px;font-size:12px;color:#8fa5c9;word-break:break-all}.foot a,.foot button{color:#c7d3ea;background:none;border:0;padding:0;font:inherit;cursor:pointer;text-decoration:underline}
.main{padding:28px 32px 48px;max-width:1320px;width:100%;min-width:0}.head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:20px;flex-wrap:wrap}
.head h1{font-size:22px;margin:0}.head p{margin:4px 0 0;color:var(--muted)}.back{display:inline-block;margin-bottom:8px;text-decoration:none;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.kpi span{color:var(--muted);font-size:13px}.kpi b{font-size:26px;display:block;line-height:1.3}.kpi small{color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-bottom:16px}.split{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:16px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}.panel h2{font-size:15px;margin:0 0 12px}.panel h3{font-size:13px;color:var(--muted);margin:18px 0 6px;font-weight:600}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.spread{display:flex;justify-content:space-between;gap:12px;align-items:center}.muted{color:var(--muted)}.small{font-size:12px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}.dot.ok{background:#12b76a}.dot.warn{background:#f79009}.dot.bad{background:#f04438}
.source{padding:12px 0;border-bottom:1px solid #f0f2f5}.source:last-child{border-bottom:0;padding-bottom:0}.source:first-of-type{padding-top:0}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.green{background:#ecfdf3;color:#067647}.amber{background:#fffaeb;color:#b54708}.red{background:#fef3f2;color:#b42318}.blue{background:#eff8ff;color:#175cd3}
.gray{background:#f2f4f7;color:#475467}.purple{background:#f4f3ff;color:#5925dc}.teal{background:#f0fdf9;color:#107569}
.bar{height:8px;background:#eef1f6;border-radius:999px;overflow:hidden;display:flex;margin:10px 0 8px}.bar i{display:block;height:100%}.bar .done{background:#12b76a}.bar .fallback{background:#98a2b3}
.notice{padding:12px 14px;border-radius:10px;margin-bottom:16px}.notice.red{border:1px solid #fda29b}.notice.green{border:1px solid #abefc6}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}.chips{display:flex;gap:6px;flex-wrap:wrap}
.chips a{display:inline-flex;gap:6px;padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:#fff;color:#344054;text-decoration:none;font-size:13px}
.chips a.on{background:var(--brand);border-color:var(--brand);color:#fff}.chips a em{font-style:normal;opacity:.65}.sep{width:1px;height:22px;background:var(--line)}
.search{display:flex;gap:6px;margin-left:auto}.search input{padding:7px 10px;border:1px solid #d0d5dd;border-radius:8px;font:inherit;width:240px;max-width:60vw}
.btn{display:inline-block;border:0;border-radius:8px;padding:8px 14px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;background:var(--brand);color:#fff;white-space:nowrap}
.btn.sm{padding:4px 10px;font-size:12px}.btn.ghost{background:#fff;border:1px solid #d0d5dd;color:#344054}.btn.danger{background:#fff;color:#b42318;border:1px solid #fda29b}
.tablebox{background:#fff;border:1px solid var(--line);border-radius:12px;overflow-x:auto}table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:10px 14px;border-bottom:1px solid var(--line);background:#f9fafb;white-space:nowrap}
td{padding:11px 14px;border-bottom:1px solid #f0f2f5;vertical-align:middle}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#f9fbff}
.nowrap{white-space:nowrap}.title a{color:var(--text);font-weight:600;text-decoration:none}.title a:hover{color:var(--brand)}
.title small{display:block;color:var(--muted);font-weight:400;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:640px}
.empty{padding:32px;text-align:center;color:var(--muted)}.pager{display:flex;justify-content:space-between;align-items:center;margin-top:12px;color:var(--muted)}
.flash{background:#ecfdf3;color:#067647;border:1px solid #abefc6;padding:10px 14px;border-radius:10px;margin-bottom:16px}
dl.kv{display:grid;grid-template-columns:88px 1fr;gap:8px 12px;margin:0}dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;word-break:break-word}
ul.plain{margin:0;padding-left:18px}ul.plain li{margin:3px 0}.summary{font-size:15px;line-height:1.75;margin:0}
pre.text{white-space:pre-wrap;font:13px/1.7 inherit;background:#f9fafb;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:420px;overflow:auto;margin:10px 0 0}
details summary{cursor:pointer;color:var(--brand);font-weight:600}
@media(max-width:900px){.shell{grid-template-columns:minmax(0,1fr)}.side{position:static;height:auto;flex-direction:row;align-items:center;padding:10px 12px;overflow-x:auto;white-space:nowrap}
.logo{padding:0 10px 0 4px;font-size:16px}.logo small{display:none}.side nav{display:flex;gap:2px}.side nav a{margin:0;padding:7px 10px}
.foot{margin:0 0 0 auto;padding:0 4px}.foot span,.foot .wide-only{display:none}
.main{padding:18px 14px 40px}.grid,.split{grid-template-columns:minmax(0,1fr)}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
.search{margin-left:0;width:100%}.search input{flex:1;width:auto}.head h1{font-size:20px}}
"""


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def badge(text: str, tone: str) -> str:
    return f'<span class="badge {tone}">{esc(text)}</span>'


def source_badge(source_key: str) -> str:
    label, tone = SOURCE_SHORT.get(source_key, (source_key, "gray"))
    return badge(label, tone)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def local(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    when = _parse(value)
    return when.astimezone(SEOUL).strftime(fmt) if when else "—"


def ago(value: str | None) -> str:
    """Relative time with the exact Seoul time on hover."""
    when = _parse(value)
    if not when:
        return "—"
    seconds = (now_utc() - when).total_seconds()
    future, seconds = seconds < 0, abs(seconds)
    if seconds < 60:
        text = "刚刚"
    elif seconds < 3600:
        text = f"{int(seconds // 60)} 分钟"
    elif seconds < 86400:
        text = f"{int(seconds // 3600)} 小时"
    elif seconds < 7 * 86400:
        text = f"{int(seconds // 86400)} 天"
    else:
        return f'<span title="{local(value)}">{local(value, "%m-%d")}</span>'
    if text != "刚刚":
        text += "后" if future else "前"
    return f'<span title="{local(value)}">{text}</span>'


def _today_start() -> str:
    today = now_utc().astimezone(SEOUL).date()
    return iso_utc(datetime.combine(today, time(0, 0), SEOUL))


def _url(path: str, args: dict) -> str:
    query = urlencode({key: value for key, value in args.items() if value})
    return f"{path}?{query}" if query else path


def _chips(path: str, args: dict, key: str, options: list[tuple[str, str, int | None]]) -> str:
    current = args.get(key) or ""
    links = []
    for value, label, count in options:
        target = _url(path, {**args, key: value, "page": ""})
        count_html = f" <em>{count}</em>" if count is not None else ""
        links.append(f'<a class="{"on" if value == current else ""}" href="{esc(target)}">{esc(label)}{count_html}</a>')
    return f'<div class="chips">{"".join(links)}</div>'


def _search(path: str, args: dict, placeholder: str) -> str:
    hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
                     for k, v in args.items() if v and k not in ("q", "page"))
    return (f'<form class="search" method="get" action="{path}">{hidden}'
            f'<input type="search" name="q" value="{esc(args.get("q", ""))}" placeholder="{esc(placeholder)}">'
            f'<button class="btn ghost">搜索</button></form>')


def _analysis(result_json: str | None) -> dict:
    return json.loads(result_json) if result_json else {}


def _explain(message: str | None) -> str:
    """Plain-language version of the common failures, so the overview reads without decoding raw errors."""
    message = message or ""
    if "insufficient_quota" in message:
        return "OpenAI 余额不足（充值后自动恢复）"
    if "timed out" in message:
        return "请求超时：" + message[:100]
    return message[:140]


def shell(email: str, csrf: str, active: str, title: str, subtitle: str, content: str, flash: str = "") -> str:
    nav = "".join(f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>'
                  for href, label in (("/admin", "总览"), ("/admin/notices", "公告"), ("/admin/subscribers", "订阅用户")))
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{esc(title)} · PNU Notice 管理后台</title><style>{ADMIN_STYLE}</style><script src="/static/admin.js" defer></script></head>
    <body><div class="shell"><aside class="side"><a class="logo" href="/admin">PNU Notice<small>管理后台</small></a><nav>{nav}</nav>
    <div class="foot"><span>{esc(email)}<br></span><span class="wide-only"><a href="/">打开网站</a> · <a href="/account">我的账户</a> · </span>
    <form method="post" action="/logout" style="display:inline"><input type="hidden" name="csrf" value="{csrf}"><button>退出</button></form></div></aside>
    <main class="main"><div class="head"><div><h1>{esc(title)}</h1>{f"<p>{subtitle}</p>" if subtitle else ""}</div></div>
    {flash_html}{content}</main></div></body></html>"""


def overview(db: Database, settings: Settings) -> tuple[str, str, str]:
    now, today = iso_utc(), _today_start()
    with db.connect() as conn:
        subscribers = dict(conn.execute("SELECT status,COUNT(*) FROM subscribers GROUP BY status").fetchall())
        new_today = conn.execute("SELECT COUNT(*) FROM notices WHERE historical_import=0 AND created_at>=?", (today,)).fetchone()[0]
        ai = dict(conn.execute("SELECT ai_status,COUNT(*) FROM notices GROUP BY ai_status").fetchall())
        sent_today = conn.execute("SELECT COUNT(*) FROM notification_deliveries WHERE status='sent' AND sent_at>=?",
                                  (today,)).fetchone()[0]
        pending = conn.execute(
            """SELECT notification_type,COUNT(*),MIN(scheduled_at) FROM notification_deliveries
               WHERE status IN ('pending','retrying') GROUP BY notification_type ORDER BY MIN(scheduled_at)""").fetchall()
        runs = {row["source_key"]: row for row in conn.execute(
            "SELECT * FROM crawl_runs WHERE id IN (SELECT MAX(id) FROM crawl_runs GROUP BY source_key)")}
        counts = {row[0]: row[1:] for row in conn.execute(
            "SELECT source_key,COUNT(*),SUM(historical_import=0) FROM notices GROUP BY source_key")}
        latest = {}
        for source in SOURCES:
            latest[source.key] = conn.execute(
                """SELECT n.id,n.original_title,n.published_at,a.result_json FROM notices n LEFT JOIN ai_analyses a
                   ON a.notice_id=n.id AND a.content_hash=n.content_hash WHERE n.source_key=?
                   ORDER BY n.published_at DESC LIMIT 1""", (source.key,)).fetchone()
        # Paused only if no real analysis has succeeded since the last out-of-credit error.
        quota_paused = conn.execute(
            """SELECT MAX(occurred_at) FROM job_failures WHERE component='ai_quota' AND occurred_at>
               COALESCE((SELECT MAX(created_at) FROM ai_analyses WHERE model!='fallback-no-ai'),'')""").fetchone()[0]
        waiting = conn.execute("SELECT MIN(ai_next_attempt_at) FROM notices WHERE ai_status='retrying' AND ai_next_attempt_at>?",
                               (now,)).fetchone()[0]
        # Group by the start of the message too, so different failures of one component are not merged into one row.
        problems = conn.execute(
            """SELECT component,COUNT(*) AS n,MAX(occurred_at) AS latest,MAX(error_message) AS message
               FROM job_failures WHERE occurred_at>? GROUP BY component,
               CASE WHEN error_message LIKE '%insufficient_quota%' THEN 'quota' ELSE substr(error_message,1,40) END
               ORDER BY latest DESC LIMIT 8""",
            (iso_utc(now_utc() - timedelta(hours=24)),)).fetchall()

    ai_total = sum(ai.values()) or 1
    ai_queue = ai.get("pending", 0) + ai.get("retrying", 0)
    kpis = [
        ("接收中的订阅者", subscribers.get("active", 0), f'待验证 {subscribers.get("pending", 0)} · 已退订 {subscribers.get("unsubscribed", 0)}'),
        ("今天的新公告", new_today, "不含历史导入"),
        ("AI 排队", ai_queue, f'已分析 {ai.get("completed", 0)} 条'),
        ("今天发出的邮件", sent_today, "含验证邮件"),
        ("待发送", sum(row[1] for row in pending), f'下一次汇总 {local(iso_utc(next_digest_slot(now_utc(), settings.digest_hours)), "%m-%d %H:%M")}'),
    ]
    kpi_html = '<div class="kpis">' + "".join(
        f'<div class="kpi"><span>{label}</span><b>{value}</b><small>{hint}</small></div>' for label, value, hint in kpis) + "</div>"

    alert_html = ""
    if quota_paused and waiting:
        alert_html = (f'<div class="notice red"><b>OpenAI 余额不足，AI 分析已暂停。</b> 充值后会在 {ago(waiting)} 自动恢复；'
                      f'暂停期间新公告会等待分析，不会降级。</div>')

    poll = settings.poll_interval_minutes
    source_html = ""
    for source in SOURCES:
        run = runs.get(source.key)
        total, fresh = counts.get(source.key, (0, 0))
        if not run:
            dot, state = "warn", "还没有抓取记录"
        else:
            age = (now_utc() - _parse(run["started_at"])).total_seconds() / 60
            if run["status"] == "failed" or age > poll * 4:
                dot = "bad"
            elif run["status"] == "partial" or age > poll * 2:
                dot = "warn"
            else:
                dot = "ok"
            state = (f'{ago(run["started_at"])}检查 · '
                     + {"completed": "正常", "partial": "部分详情页失败", "failed": "失败", "running": "进行中"}.get(run["status"], run["status"])
                     + (f' · 新增 {run["new_items"]}' if run["new_items"] else ""))
        item = latest[source.key]
        latest_html = ""
        if item:
            title = _analysis(item["result_json"]).get("title_zh") or item["original_title"]
            latest_html = (f'<div class="small muted" style="margin-top:4px">最新：<a href="/admin/notices/{item["id"]}">{esc(title)}</a>'
                           f' · {ago(item["published_at"])}</div>')
        source_html += (f'<div class="source"><div class="spread"><div class="row"><span class="dot {dot}"></span>'
                        f'<b>{esc(source.display_name)}</b></div><span class="small muted">{total} 条 · 新 {fresh or 0}</span></div>'
                        f'<div class="small muted" style="margin-top:2px">{state}</div>{latest_html}</div>')

    done, fallback = ai.get("completed", 0), ai.get("failed", 0)
    ai_html = (f'<div class="spread"><span>模型 <b>{esc(settings.openai_model)}</b></span>'
               f'<span class="small muted">共 {sum(ai.values())} 条</span></div>'
               f'<div class="bar"><i class="done" style="width:{done / ai_total * 100:.1f}%"></i>'
               f'<i class="fallback" style="width:{fallback / ai_total * 100:.1f}%"></i></div>'
               f'<div class="row small">{badge(f"已分析 {done}", "green")}{badge(f"排队 {ai_queue}", "amber")}'
               f'{badge(f"原文降级 {fallback}", "gray")}</div>')

    if pending:
        pending_rows = "".join(f'<tr><td>{DELIVERY_TYPES.get(row[0], esc(row[0]))}</td><td class="nowrap">{row[1]}</td>'
                               f'<td class="nowrap">{ago(row[2])}</td></tr>' for row in pending)
        pending_html = f'<table><thead><tr><th>类型</th><th>数量</th><th>最早发送</th></tr></thead><tbody>{pending_rows}</tbody></table>'
    else:
        pending_html = '<p class="muted" style="margin:0">没有待发送的邮件。</p>'
    schedule = "、".join(f"{hour}:00" for hour in settings.digest_hours)
    weekday = "一二三四五六日"[settings.weekly_digest_weekday]
    delivery_html = (pending_html + f'<p class="small muted" style="margin:12px 0 0">重要公告立即发送；普通公告工作日 {schedule} 汇总；'
                     f'低优先级每周{weekday} {settings.weekly_digest_hour}:00。</p>')

    if problems:
        rows = "".join(
            f'<tr><td class="nowrap">{COMPONENTS.get(row["component"], esc(row["component"]))}</td><td class="nowrap">{row["n"]} 次</td>'
            f'<td class="nowrap">{ago(row["latest"])}</td><td class="small muted">{esc(_explain(row["message"]))}</td></tr>'
            for row in problems)
        problems_html = f'<div class="tablebox"><table><thead><tr><th>类型</th><th>次数</th><th>最近</th><th>最近一次的信息</th></tr></thead><tbody>{rows}</tbody></table></div>'
    else:
        problems_html = '<p class="muted" style="margin:0"><span class="dot ok"></span> 最近 24 小时没有问题。</p>'

    content = (kpi_html + alert_html
               + f'<div class="grid"><div class="panel"><h2>公告来源</h2>{source_html}</div>'
               + f'<div><div class="panel"><h2>AI 分析</h2>{ai_html}</div><div class="panel"><h2>邮件发送</h2>{delivery_html}</div></div></div>'
               + f'<div class="panel"><h2>最近 24 小时的问题</h2>{problems_html}</div>')
    return "总览", f'{local(now, "%Y年%m月%d日 %H:%M")}（首尔时间）', content


def subscribers_page(db: Database, csrf: str, args: dict) -> tuple[str, str, str]:
    status, q = args.get("status") or "", (args.get("q") or "").strip()
    where, params = [], []
    if status in STATUS_LABELS:
        where.append("s.status=?")
        params.append(status)
    if q:
        where.append("s.email_normalized LIKE ?")
        params.append(f"%{q.casefold()}%")
    with db.connect() as conn:
        counts = dict(conn.execute("SELECT status,COUNT(*) FROM subscribers GROUP BY status").fetchall())
        rows = conn.execute(
            f"""SELECT s.*,a.password_hash IS NOT NULL AS has_password,a.role,
               (SELECT GROUP_CONCAT(source_key) FROM subscriptions WHERE subscriber_id=s.id AND enabled=1) AS sources,
               (SELECT MAX(immediate_enabled) FROM subscriptions WHERE subscriber_id=s.id) AS immediate,
               (SELECT MAX(daily_digest_enabled) FROM subscriptions WHERE subscriber_id=s.id) AS daily,
               (SELECT MAX(weekly_digest_enabled) FROM subscriptions WHERE subscriber_id=s.id) AS weekly,
               (SELECT COUNT(*) FROM notification_deliveries WHERE subscriber_id=s.id AND status='sent'
                AND notification_type!='verification') AS sent
               FROM subscribers s LEFT JOIN accounts a ON a.subscriber_id=s.id
               {"WHERE " + " AND ".join(where) if where else ""} ORDER BY s.id DESC LIMIT 500""", params).fetchall()
    options = [("", "全部", sum(counts.values()))] + [(key, label, counts.get(key, 0)) for key, label in STATUS_LABELS.items()
                                                      if counts.get(key) or key in ("active", "pending")]
    toolbar = (f'<div class="toolbar">{_chips("/admin/subscribers", args, "status", options)}'
               f'{_search("/admin/subscribers", args, "搜索邮箱")}</div>')
    if not rows:
        return "订阅用户", "", toolbar + '<div class="tablebox"><div class="empty">没有符合条件的订阅用户</div></div>'
    body = ""
    for row in rows:
        sources = " ".join(source_badge(key) for key in (row["sources"] or "").split(",") if key)
        modes = "、".join(label for label, on in (("即时", row["immediate"]), ("汇总", row["daily"]), ("每周", row["weekly"])) if on)
        email = esc(row["email"]) + (" " + badge("管理员", "purple") if row["role"] == "admin" else "")
        password = '<div class="small muted">已设密码</div>' if row["has_password"] else '<div class="small muted">未设密码</div>'
        actions = ""
        for action, label, show, style, confirm in (
                ("pause", "暂停", row["status"] == "active", "ghost", f"暂停 {row['email']} 的订阅？"),
                ("resume", "恢复", row["status"] in ("paused", "unsubscribed"), "ghost", f"恢复 {row['email']} 的订阅？"),
                ("unsubscribe", "退订", row["status"] in ("active", "paused"), "ghost", f"确定替 {row['email']} 退订？对方将不再收到任何公告邮件。"),
                ("delete", "删除", row["role"] != "admin", "danger",
                 f"永久删除 {row['email']} 的所有数据（邮箱、订阅、密码、发信记录）？此操作无法恢复。")):
            if show:
                actions += (f'<button class="btn sm {style}" name="action" value="{action}" data-confirm="{esc(confirm)}">{label}</button> ')
        form = (f'<form method="post" action="/admin/subscribers"><input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="id" value="{row["id"]}"><div class="row">{actions}</div></form>') if actions else ""
        body += (f'<tr><td>{email}{password}</td>'
                 f'<td>{badge(STATUS_LABELS.get(row["status"], row["status"]), STATUS_TONES.get(row["status"], "gray"))}</td>'
                 f'<td><div class="row">{sources or "—"}</div></td><td class="nowrap">{modes or "—"}</td>'
                 f'<td class="nowrap">{row["sent"]}</td><td class="nowrap">{ago(row["verified_at"] or row["created_at"])}</td><td>{form}</td></tr>')
    table = (f'<div class="tablebox"><table><thead><tr><th>邮箱</th><th>状态</th><th>订阅来源</th><th>接收方式</th>'
             f'<th>已发公告邮件</th><th>加入</th><th></th></tr></thead><tbody>{body}</tbody></table></div>')
    return "订阅用户", f"共 {sum(counts.values())} 人，其中 {counts.get('active', 0)} 人正在接收", toolbar + table


def notices_page(db: Database, args: dict) -> tuple[str, str, str]:
    source, scope, ai_state = args.get("source") or "", args.get("scope") or "", args.get("ai") or ""
    q = (args.get("q") or "").strip()
    page = max(int(args.get("page") or 1) if str(args.get("page") or "1").isdigit() else 1, 1)
    where, params = [], []
    if source in SOURCE_BY_KEY:
        where.append("n.source_key=?")
        params.append(source)
    if scope == "new":
        where.append("n.historical_import=0")
    if ai_state == "queued":
        where.append("n.ai_status IN ('pending','retrying')")
    elif ai_state in ("completed", "failed"):
        where.append("n.ai_status=?")
        params.append(ai_state)
    if q:
        where.append("(n.original_title LIKE ? OR a.result_json LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = "WHERE " + " AND ".join(where) if where else ""
    base = "FROM notices n LEFT JOIN ai_analyses a ON a.notice_id=n.id AND a.content_hash=n.content_hash"
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base} {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT n.id,n.source_key,n.original_title,n.published_at,n.ai_status,n.historical_import,a.result_json,
                (SELECT COUNT(*) FROM notification_deliveries d WHERE d.notice_id=n.id AND d.status='sent') AS sent
                {base} {clause} ORDER BY n.published_at DESC LIMIT ? OFFSET ?""",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        by_source = dict(conn.execute("SELECT source_key,COUNT(*) FROM notices GROUP BY source_key").fetchall())
        all_count = sum(by_source.values())
    toolbar = ('<div class="toolbar">'
               + _chips("/admin/notices", args, "source", [("", "全部来源", all_count)]
                        + [(s.key, SOURCE_SHORT[s.key][0], by_source.get(s.key, 0)) for s in SOURCES])
               + '<span class="sep"></span>'
               + _chips("/admin/notices", args, "scope", [("", "全部", None), ("new", "只看新公告", None)])
               + '<span class="sep"></span>'
               + _chips("/admin/notices", args, "ai", [("", "全部 AI 状态", None), ("queued", "排队中", None),
                                                      ("completed", "已分析", None), ("failed", "原文降级", None)])
               + _search("/admin/notices", args, "搜索标题或摘要") + "</div>")
    if not rows:
        return "公告", "", toolbar + '<div class="tablebox"><div class="empty">没有符合条件的公告</div></div>'
    body = ""
    for row in rows:
        analysis = _analysis(row["result_json"])
        zh = analysis.get("title_zh")
        title = esc(zh or row["original_title"])
        original = f'<small>{esc(row["original_title"])}</small>' if zh and zh != row["original_title"] else ""
        importance = IMPORTANCE.get(analysis.get("importance")) if row["ai_status"] == "completed" else None
        ai_label, ai_tone = AI_STATES.get(row["ai_status"], (row["ai_status"], "gray"))
        history = " " + badge("历史", "gray") if row["historical_import"] else ""
        body += (f'<tr><td class="title"><a href="/admin/notices/{row["id"]}">{title}</a>{history}{original}</td>'
                 f'<td>{source_badge(row["source_key"])}</td><td class="nowrap">{ago(row["published_at"])}</td>'
                 f'<td>{badge(*importance) if importance else "<span class=muted>—</span>"}</td>'
                 f'<td>{badge(ai_label, ai_tone)}</td><td class="nowrap">{row["sent"] or "—"}</td></tr>')
    pages = max(math.ceil(total / PAGE_SIZE), 1)
    prev_link = (f'<a class="btn ghost sm" href="{esc(_url("/admin/notices", {**args, "page": str(page - 1)}))}">上一页</a>'
                 if page > 1 else "")
    next_link = (f'<a class="btn ghost sm" href="{esc(_url("/admin/notices", {**args, "page": str(page + 1)}))}">下一页</a>'
                 if page < pages else "")
    table = (f'<div class="tablebox"><table><thead><tr><th>公告</th><th>来源</th><th>发布</th><th>重要度</th><th>AI</th>'
             f'<th>已发邮件</th></tr></thead><tbody>{body}</tbody></table></div>'
             f'<div class="pager"><span>共 {total} 条 · 第 {page} / {pages} 页</span><div class="row">{prev_link}{next_link}</div></div>')
    return "公告", "点任意一条查看 AI 分析结果和发信记录", toolbar + table


def notice_detail(db: Database, csrf: str, notice_id: int) -> tuple[str, str, str] | None:
    with db.connect() as conn:
        notice = conn.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
        if not notice:
            return None
        analysis_row = conn.execute("SELECT * FROM ai_analyses WHERE notice_id=? AND content_hash=?",
                                    (notice_id, notice["content_hash"])).fetchone()
        attachments = conn.execute("SELECT filename,url,extension FROM attachments WHERE notice_id=?", (notice_id,)).fetchall()
        deliveries = conn.execute(
            """SELECT notification_type,status,COUNT(*),MAX(COALESCE(sent_at,scheduled_at)) FROM notification_deliveries
               WHERE notice_id=? GROUP BY notification_type,status ORDER BY notification_type""", (notice_id,)).fetchall()
        revisions = conn.execute("SELECT COUNT(*) FROM notice_revisions WHERE notice_id=?", (notice_id,)).fetchone()[0]
    analysis = _analysis(analysis_row["result_json"]) if analysis_row else {}
    ai_label, ai_tone = AI_STATES.get(notice["ai_status"], (notice["ai_status"], "gray"))
    importance = IMPORTANCE.get(analysis.get("importance")) if notice["ai_status"] == "completed" else None
    badges = [source_badge(notice["source_key"]), badge(ai_label, ai_tone)]
    if importance:
        badges.append(badge(*importance))
    if analysis.get("status"):
        badges.append(badge(NOTICE_STATUS.get(analysis["status"], analysis["status"]), "gray"))
    if notice["historical_import"]:
        badges.append(badge("历史导入", "gray"))
    actions = (f'<div class="row" style="margin:-8px 0 18px">{"".join(badges)}<span style="flex:1"></span>'
               f'<a class="btn ghost" href="{esc(notice["original_url"])}" target="_blank" rel="noopener">查看学校原公告</a>'
               f'<form method="post" action="/admin/notices/{notice_id}"><input type="hidden" name="csrf" value="{csrf}">'
               f'<button class="btn" name="action" value="reanalyze" data-confirm="重新用 AI 分析这条公告？已经发出的邮件不会重复发送。">重新分析</button></form></div>')

    def items(values: list, empty: str) -> str:
        if not values:
            return f'<p class="muted" style="margin:0">{empty}</p>'
        return '<ul class="plain">' + "".join(f"<li>{esc(value)}</li>" for value in values) + "</ul>"

    if analysis:
        deadlines = analysis.get("deadlines") or []
        if deadlines:
            deadline_rows = "".join(
                f'<tr><td class="nowrap">{esc(item.get("type"))}</td><td class="nowrap">{local(item["datetime"]) if item.get("datetime") else "—"}</td>'
                f'<td class="small muted">{esc(item.get("original_text"))}</td></tr>' for item in deadlines)
            deadline_html = (f'<div class="tablebox"><table><thead><tr><th>类型</th><th>时间（首尔）</th><th>原文</th></tr></thead>'
                             f'<tbody>{deadline_rows}</tbody></table></div>')
        else:
            deadline_html = '<p class="muted" style="margin:0">没有识别到明确的截止时间</p>'
        main = (f'<div class="panel"><h2>AI 摘要</h2><p class="summary">{esc(analysis.get("summary_zh"))}</p>'
                f'<h3>需要做什么</h3>{items(analysis.get("actions") or [], "无需操作")}'
                f'<h3>截止时间</h3>{deadline_html}'
                f'<h3>适用对象 / 资格</h3>{items(analysis.get("eligibility") or [], "原文没有明确说明")}'
                f'<h3>所需材料</h3>{items(analysis.get("required_documents") or [], "原文没有明确说明")}'
                f'<h3>注意事项</h3>{items(analysis.get("warnings") or [], "无")}</div>')
    else:
        main = '<div class="panel"><h2>AI 摘要</h2><p class="muted" style="margin:0">这条公告还在等待 AI 分析。</p></div>'
    main += (f'<div class="panel"><details><summary>查看抓取到的原文</summary>'
             f'<pre class="text">{esc(notice["clean_text"][:6000]) or "（正文为空，可能是图片公告）"}</pre></details></div>')

    info = [("来源", esc(SOURCE_BY_KEY[notice["source_key"]].display_name)), ("发布", local(notice["published_at"])),
            ("抓取", local(notice["created_at"])), ("最近检查", ago(notice["last_checked_at"])),
            ("分类", esc(notice["category_original"] or "—")), ("作者", esc(notice["author"] or "—")),
            ("内容变更", f"{revisions} 次"), ("AI 模型", esc(analysis_row["model"]) if analysis_row else "—")]
    info_html = '<dl class="kv">' + "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in info) + "</dl>"
    if attachments:
        attachment_html = '<ul class="plain">' + "".join(
            f'<li><a href="{esc(item["url"])}" target="_blank" rel="noopener">{esc(item["filename"])}</a></li>'
            for item in attachments) + "</ul>"
    else:
        attachment_html = '<p class="muted" style="margin:0">没有附件</p>'
    if deliveries:
        delivery_rows = "".join(
            f'<tr><td>{DELIVERY_TYPES.get(row[0], esc(row[0]))}</td><td>{DELIVERY_STATUS.get(row[1], esc(row[1]))}</td>'
            f'<td class="nowrap">{row[2]}</td></tr>' for row in deliveries)
        delivery_html = f'<table><thead><tr><th>类型</th><th>状态</th><th>数量</th></tr></thead><tbody>{delivery_rows}</tbody></table>'
    elif notice["historical_import"]:
        delivery_html = '<p class="muted" style="margin:0">历史公告不发送邮件</p>'
    else:
        delivery_html = '<p class="muted" style="margin:0">还没有发信任务</p>'
    side = (f'<div class="panel"><h2>公告信息</h2>{info_html}</div><div class="panel"><h2>附件</h2>{attachment_html}</div>'
            f'<div class="panel"><h2>发信记录</h2>{delivery_html}</div>')
    title = analysis.get("title_zh") or notice["original_title"]
    subtitle = '<a class="back" href="/admin/notices">← 返回公告列表</a><br>' + esc(notice["original_title"])
    return title, subtitle, actions + f'<div class="split"><div>{main}</div><div>{side}</div></div>'


def reanalyze(db: Database, notice_id: int) -> None:
    """Queues a fresh AI analysis; delivery dedupe keys keep any already-sent email from going out twice."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM ai_analyses WHERE notice_id=?", (notice_id,))
        conn.execute("""UPDATE notices SET ai_status='pending',ai_attempts=0,ai_next_attempt_at=NULL,
                        processing_status='ai_pending' WHERE id=?""", (notice_id,))
