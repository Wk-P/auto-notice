from __future__ import annotations

import html
import json
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from . import admin
from .accounts import AccountService, check_password
from .config import Settings
from .security import verify_turnstile
from .sources import SOURCES
from .timeutil import SEOUL
from .subscriptions import SubscriptionService

SESSION_COOKIE = "pnu_session"
CSRF_PROTECTED = {"/logout", "/account", "/account/password", "/account/delete", "/admin/subscribers", "/admin/notices"}
TURNSTILE_ACTION = "subscribe"
STATUS_LABELS = admin.STATUS_LABELS
ACTION_MESSAGES = {"save": "订阅设置已保存。", "pause": "订阅已暂停，恢复前不会收到邮件。", "resume": "订阅已恢复。",
                   "unsubscribe": "你已退订，不会再收到公告邮件。"}

STYLE = """
:root{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#17213b;background:#f5f7fb}
*{box-sizing:border-box}body{margin:0}a{color:#174b9b}.nav{padding:16px 6vw;background:#fff;border-bottom:1px solid #e6e9f0;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-weight:800;color:#174b9b;text-decoration:none;font-size:20px}.links{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.links a,.linkbtn{color:#344054;text-decoration:none;font-size:15px;background:none;border:0;padding:0;cursor:pointer;font-family:inherit}.links a.pill{background:#174b9b;color:#fff;padding:8px 16px;border-radius:999px}
.wrap{max-width:720px;margin:48px auto;padding:0 16px}.wrap.wide{max-width:1180px}
.nav{position:sticky;top:0;z-index:10;background:#ffffffe6;backdrop-filter:saturate(180%) blur(12px);-webkit-backdrop-filter:saturate(180%) blur(12px)}
.brand{display:inline-flex;align-items:center;gap:10px;letter-spacing:-.01em}
.home-hero{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:40px;align-items:center;padding:56px;border-radius:28px;color:#fff;
background:radial-gradient(1200px 400px at 10% -20%,#3b82f633,transparent),radial-gradient(800px 500px at 110% 120%,#ffcf4a26,transparent),linear-gradient(135deg,#0a2a57,#123f7f 55%,#1966bd);
box-shadow:0 30px 60px -20px #0a2a5780;position:relative;overflow:hidden}
.home-hero:before{content:"";position:absolute;inset:0;background-image:linear-gradient(#ffffff0d 1px,transparent 1px),linear-gradient(90deg,#ffffff0d 1px,transparent 1px);background-size:36px 36px;mask-image:linear-gradient(180deg,#000,transparent 85%);-webkit-mask-image:linear-gradient(180deg,#000,transparent 85%)}
.home-hero>*{position:relative}.eyebrow{display:inline-flex;gap:8px;align-items:center;padding:6px 12px;border-radius:999px;background:#ffffff1a;border:1px solid #ffffff2e;font-size:13px;color:#dbe8ff;margin-bottom:20px}
.home-hero h1{font-size:clamp(34px,5vw,56px);line-height:1.08;letter-spacing:-.02em;margin:0 0 18px}.home-hero h1 em{font-style:normal;color:#ffcf4a}
.live{display:flex;align-items:center;gap:8px;margin-top:22px;font-size:14px;color:#b9cdf0}.pulse{width:8px;height:8px;border-radius:50%;background:#34d399;box-shadow:0 0 0 4px #34d39933}
.mail{background:#fff;color:#17213b;border-radius:18px;padding:20px 22px;box-shadow:0 24px 50px -12px #00000059;transform:rotate(1.5deg)}
.mail .from{display:flex;gap:10px;align-items:center;font-size:13px;color:#667085;border-bottom:1px solid #eef1f6;padding-bottom:12px;margin-bottom:12px}
.mail h3{font-size:16px;margin:0 0 8px;line-height:1.4}.mail p{font-size:13px;color:#475467;line-height:1.7;margin:0 0 10px}.tag{display:inline-block;font-size:12px;font-weight:700;padding:2px 8px;border-radius:999px;background:#fff1e0;color:#b54708;margin-right:6px}
.tag.blue{background:#eff8ff;color:#175cd3}.mail .due{display:flex;justify-content:space-between;background:#f8fafc;border-radius:10px;padding:10px 12px;font-size:13px}
.strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0 56px}.strip div{background:#fff;border:1px solid #e2e6ef;border-radius:16px;padding:18px 20px}
.strip b{display:block;font-size:24px;letter-spacing:-.01em;color:#0a2a57}.strip span{color:#667085;font-size:14px}
.section-title{text-align:center;margin:0 0 8px;font-size:28px;letter-spacing:-.01em}.section-sub{text-align:center;color:#667085;margin:0 0 28px}
.cards3,.cards4{display:grid;gap:16px;margin-bottom:56px}.cards3{grid-template-columns:repeat(3,minmax(0,1fr))}.cards4{grid-template-columns:repeat(4,minmax(0,1fr))}
.feature{background:#fff;border:1px solid #e2e6ef;border-radius:18px;padding:24px;transition:transform .2s,box-shadow .2s}.feature:hover{transform:translateY(-3px);box-shadow:0 16px 32px -16px #182b4d40}
.feature .icon{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:#eef4ff;margin-bottom:14px}.feature h3{margin:0 0 6px;font-size:17px}.feature p{margin:0;color:#667085;line-height:1.65;font-size:14px}
.num{font-size:13px;font-weight:800;color:#174b9b;letter-spacing:.08em;margin-bottom:10px}
.cta{display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap;padding:36px 40px;border-radius:24px;background:linear-gradient(135deg,#fff6d6,#ffe8a3);margin-bottom:24px}.cta h2{margin:0 0 6px;font-size:24px}.cta p{margin:0;color:#6e4b00}
.site-footer{background:#0a1f40;color:#9fb3d6;margin-top:64px;padding:48px 6vw 28px;font-size:14px}.footer-inner{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:1.4fr repeat(3,1fr);gap:32px}
.site-footer h4{color:#fff;font-size:14px;margin:0 0 12px}.site-footer a{color:#c9d6ee;text-decoration:none;display:block;margin:7px 0}.site-footer a:hover{color:#fff}
.site-footer .about{line-height:1.7;margin:12px 0 0;max-width:320px}.footer-brand{display:flex;gap:10px;align-items:center;color:#fff;font-weight:800;font-size:18px}
.copyright{max-width:1180px;margin:36px auto 0;padding-top:20px;border-top:1px solid #ffffff1a;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13px;color:#7f93b8}
@media(max-width:860px){.home-hero{grid-template-columns:1fr;padding:32px 24px}.mail{transform:none}.strip,.cards4{grid-template-columns:repeat(2,minmax(0,1fr))}.cards3{grid-template-columns:1fr}
.footer-inner{grid-template-columns:1fr 1fr}.cta{padding:28px 24px}}
.hero{padding:42px;background:linear-gradient(135deg,#0d3d7a,#1966bd);color:#fff;border-radius:24px;box-shadow:0 18px 45px #173c7826}
.hero h1{font-size:clamp(30px,6vw,52px);line-height:1.1;margin:0 0 18px}.lead{font-size:18px;line-height:1.7;color:#dfeeff;margin:0 0 26px}
h1{font-size:30px;margin:0 0 8px}h2{font-size:20px;margin:0 0 6px}.sub{color:#667085;margin:0 0 24px;line-height:1.6}
.btn{display:inline-block;background:#ffcf4a;color:#17213b;border:0;border-radius:10px;padding:12px 22px;font-weight:750;text-decoration:none;cursor:pointer;font-size:16px;font-family:inherit}
.btn.secondary{background:#e8edf6}.btn.ghost{background:transparent;color:#fff;border:1px solid #ffffff88}.btn.danger{background:#fff;color:#b42318;border:1px solid #f2b8b5}
.card{background:#fff;border:1px solid #e2e6ef;border-radius:18px;padding:28px;box-shadow:0 12px 32px #182b4d12;margin-bottom:20px}
.field{margin:18px 0}form>.field:first-child{margin-top:0}.field>label,.legend{display:block;font-weight:650;margin-bottom:6px}
input[type=email],input[type=password]{width:100%;padding:13px;border:1px solid #bfc7d6;border-radius:9px;font-size:16px;font-family:inherit}
.option{display:flex;gap:10px;padding:8px 0;line-height:1.45}.option input{margin-top:4px}.hint{color:#667085;font-size:14px}
.muted{color:#667085;line-height:1.6}.alert{padding:13px 16px;border-radius:9px;background:#fff2d1;color:#6e4b00;margin-bottom:20px}.alert.success{background:#e4f7ed;color:#17633a}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.status{display:inline-block;padding:3px 10px;border-radius:999px;font-size:13px;background:#e4f7ed;color:#17633a}.status.off{background:#f2f4f7;color:#475467}
.steps{display:flex;gap:8px;margin:0 0 26px;padding:0;list-style:none;flex-wrap:wrap}.steps li{flex:1;min-width:150px;padding:10px 14px;border-radius:12px;background:#e8edf6;color:#667085;font-size:14px}
.steps li b{display:block;font-size:13px;font-weight:600;color:inherit;opacity:.8}.steps li.on{background:#174b9b;color:#fff}.steps li.done{background:#e4f7ed;color:#17633a}
.big{font-size:18px;line-height:1.7}.checklist{padding-left:20px;color:#475467;line-height:1.9}

.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 20px}.tabs a{padding:8px 14px;border-radius:999px;background:#e8edf6;color:#344054;text-decoration:none;font-size:14px}.tabs a.on{background:#174b9b;color:#fff}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:28px}.stat{background:#fff;border:1px solid #e2e6ef;border-radius:14px;padding:16px}.stat b{display:block;font-size:28px}.stat span{color:#667085;font-size:14px}
.wide h2{margin:28px 0 12px}.table-wrap{overflow-x:auto;background:#fff;border:1px solid #e2e6ef;border-radius:14px}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:10px 12px;border-bottom:1px solid #eef1f6;text-align:left;vertical-align:top}th{background:#f8fafc;white-space:nowrap}td a{color:#174b9b}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-all}form.inline{display:inline}.mini{font-size:13px;padding:5px 10px;border-radius:7px;border:1px solid #c9d2e3;background:#fff;cursor:pointer;margin:0 4px 4px 0}
"""


@dataclass
class View:
    """What every page needs to render the shared chrome: who is signed in and the CSRF token for their forms."""
    settings: Settings
    account: dict | None = None
    csrf: str | None = None

    def hidden_csrf(self) -> str:
        return f'<input type="hidden" name="csrf" value="{self.csrf}">' if self.csrf else ""


LOGO = ('<svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#174b9b"/>'
        '<path d="M16 7.5a6 6 0 0 0-6 6v4.2l-1.6 2.6c-.4.7.1 1.5.9 1.5h13.4c.8 0 1.3-.8.9-1.5L22 17.7v-4.2a6 6 0 0 0-6-6Z" fill="#fff"/>'
        '<path d="M13.6 23.4a2.5 2.5 0 0 0 4.8 0" stroke="#ffcf4a" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>')


def site_footer() -> str:
    year = datetime.now(SEOUL).year
    sources = "".join(f'<a href="{html.escape(source.page_url)}" target="_blank" rel="noopener">{html.escape(source.display_name)} ↗</a>'
                      for source in SOURCES)
    return f"""<footer class="site-footer"><div class="footer-inner">
    <div><div class="footer-brand">{LOGO}PNU Notice</div>
    <p class="about">自动整理釜山大学计算机学部和国际处的官方公告，生成中文摘要，在重要更新和截止日期之前发到你的邮箱。</p></div>
    <div><h4>服务</h4><a href="/subscribe">订阅公告</a><a href="/login">登录</a><a href="/account">管理我的订阅</a><a href="/forgot-password">设置密码</a></div>
    <div><h4>官方公告来源</h4>{sources}</div>
    <div><h4>说明</h4><a href="/privacy">隐私与账户说明</a><a href="/privacy">退订与注销</a></div></div>
    <div class="copyright"><span>© {year} PNU Notice. All rights reserved.</span>
    <span>学生独立项目，与釜山大学官方无关 · 摘要由 AI 生成，请以学校原公告为准</span></div></footer>"""


def layout(view: View, content: str, title: str, wide: bool = False) -> str:
    if view.account:
        links = '<a href="/account">我的订阅</a>'
        if view.account["role"] == "admin":
            links += '<a href="/admin">管理后台</a>'
        links += f'<form method="post" action="/logout" class="inline">{view.hidden_csrf()}<button class="linkbtn">退出</button></form>'
    else:
        links = '<a href="/login">登录</a><a class="pill" href="/subscribe">订阅</a>'
    page_title = "PNU Notice" if title == "PNU Notice" else f"{title} · PNU Notice"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{html.escape(page_title)}</title><style>{STYLE}</style></head><body><nav class="nav"><a class="brand" href="/">{LOGO}PNU Notice</a>
    <div class="links">{links}</div></nav>
    <div class="wrap{' wide' if wide else ''}">{content}</div>{site_footer()}</body></html>"""


def alert(message: str, error: bool = False) -> str:
    return f'<div class="alert{"" if error else " success"}">{html.escape(message)}</div>' if message else ""


def preference_fields(settings: Settings, sources: set[str], immediate: bool, daily: bool, weekly: bool) -> str:
    """The one place subscription choices are rendered, so signup and every management page read the same."""
    weekday = "一二三四五六日"[settings.weekly_digest_weekday]
    source_html = "".join(
        f'<label class="option"><input type="checkbox" name="source" value="{source.key}" '
        f'{"checked" if source.key in sources else ""}><span>{html.escape(source.display_name)}</span></label>'
        for source in SOURCES)
    frequencies = (
        ("immediate", immediate, "重要公告立即发送", "签证、奖学金、毕业等重要事项，以及截止日期提醒"),
        ("daily", daily, f"普通公告在工作日 {'、'.join(f'{hour}:00' for hour in settings.digest_hours)} 汇总发送",
         "周一至周五每天最多几封，发布后通常 3 小时内收到；周末的公告合并到周一第一封"),
        ("weekly", weekly, f"低优先级公告每周{weekday} {settings.weekly_digest_hour}:00 汇总", "讲座、活动、宣传等信息"),
    )
    frequency_html = "".join(
        f'<label class="option"><input type="checkbox" name="{name}" {"checked" if checked else ""}>'
        f'<span>{label}<br><span class="hint">{hint}</span></span></label>' for name, checked, label, hint in frequencies)
    return (f'<div class="field"><span class="legend">想接收的公告</span>{source_html}</div>'
            f'<div class="field"><span class="legend">接收方式</span>{frequency_html}</div>')


def subscription_cards(view: View, subscriber: dict, subscriptions: list[dict], action: str, hidden: str,
                       show_email: bool = True) -> str:
    """Settings, then pause/unsubscribe in a separate form so an irreversible action is never next to “save”."""
    enabled = {item["source_key"] for item in subscriptions if item["enabled"]}
    prefs = preference_fields(view.settings, enabled,
                              any(item["immediate_enabled"] for item in subscriptions),
                              any(item["daily_digest_enabled"] for item in subscriptions),
                              any(item["weekly_digest_enabled"] for item in subscriptions))
    status = subscriber["status"]
    badge = f'<span class="status{"" if status == "active" else " off"}">{STATUS_LABELS.get(status, status)}</span>'
    if status in ("paused", "unsubscribed"):
        state_text = "恢复后会继续按上面的设置接收公告。" if status == "paused" else "你已退订。重新启用后会按上面的设置接收公告。"
        buttons = '<button class="btn" name="action" value="resume">重新启用订阅</button>'
    else:
        state_text = "暂停后不会收到任何邮件，随时可以恢复；退订会停止所有公告邮件。"
        buttons = ('<button class="btn secondary" name="action" value="pause">暂停订阅</button>'
                   '<button class="btn danger" name="action" value="unsubscribe">退订</button>')
    email = f'<p class="sub">{html.escape(subscriber["email"])}</p>' if show_email else '<p class="sub"></p>'
    return f"""<div class="card"><h2>订阅设置 {badge}</h2>{email}
    <form method="post" action="{action}">{hidden}{prefs}<button class="btn" name="action" value="save">保存设置</button></form></div>
    <div class="card"><h2>暂停或退订</h2><p class="sub">{state_text}</p>
    <form method="post" action="{action}">{hidden}<div class="row">{buttons}</div></form></div>"""


def new_password_fields() -> str:
    return """<div class="field"><label>新密码</label><input type="password" name="new_password" required minlength="8" autocomplete="new-password">
    <span class="hint">至少 8 位</span></div>
    <div class="field"><label>再次输入新密码</label><input type="password" name="confirm_password" required minlength="8" autocomplete="new-password"></div>"""


ICONS = {
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" fill="none" stroke="#174b9b" stroke-width="1.8" stroke-linejoin="round"/>',
    "clock": '<circle cx="12" cy="12" r="9" fill="none" stroke="#174b9b" stroke-width="1.8"/><path d="M12 7v5l3 2" fill="none" stroke="#174b9b" stroke-width="1.8" stroke-linecap="round"/>',
    "text": '<path d="M4 6h16M4 11h16M4 16h10" stroke="#174b9b" stroke-width="1.8" stroke-linecap="round"/>',
    "shield": '<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3Z" fill="none" stroke="#174b9b" stroke-width="1.8" stroke-linejoin="round"/>',
}


def icon(name: str) -> str:
    return f'<div class="icon"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">{ICONS[name]}</svg></div>'


def home_page(view: View, deleted: bool = False, stats: dict | None = None) -> str:
    notice = alert("你的账户和所有数据已经永久删除。如果以后还想接收公告，随时可以重新订阅。") if deleted else ""
    stats = stats or {}
    if view.account:
        actions = '<a class="btn" href="/account">管理我的订阅</a>'
    else:
        actions = '<div class="row"><a class="btn" href="/subscribe">免费订阅</a><a class="btn ghost" href="/login">登录</a></div>'
    live = ""
    if stats.get("notices"):
        checked = f' · 最近一次检查 {stats["checked"]}' if stats.get("checked") else ""
        live = f'<div class="live"><span class="pulse"></span>已收录 {stats["notices"]} 条官方公告{checked}</div>'
    hours = "、".join(f"{hour}:00" for hour in view.settings.digest_hours)
    return notice + f"""<section class="home-hero"><div>
    <span class="eyebrow">釜山大学 · 计算机学部 &amp; 国际处</span>
    <h1>重要公告，<br><em>不再错过</em></h1>
    <p class="lead">自动整理学校官方公告，生成中文摘要。签证、奖学金、毕业等重要事项发布后立即提醒，其余公告工作日每天汇总送达。</p>
    {actions}{live}</div>
    <div class="mail" aria-hidden="true"><div class="from">{LOGO}<div><b style="color:#17213b">PNU Notice</b><br>示例邮件</div></div>
    <h3><span class="tag">重要</span>理工科研究生活补助追加申请通知</h3>
    <p>面向理工科全日制研究生（含修业期满后研究生）。有意申请者须由导师或本人填写申请表，并按附件要求提交资格证明材料。</p>
    <div class="due"><span>申请截止</span><b>10月12日 13:00</b></div>
    <p style="margin:10px 0 0;font-size:12px"><span class="tag blue">计算机大学院</span>查看学校原公告 →</p></div></section>

    <div class="strip"><div><b>3 个</b><span>官方公告来源</span></div><div><b>15 分钟</b><span>检查一次学校网站</span></div>
    <div><b>3 小时内</b><span>工作日普通公告送达</span></div><div><b>中文摘要</b><span>每条都附学校原文链接</span></div></div>

    <h2 class="section-title">三步开始</h2><p class="section-sub">不用安装 App，也不用注册复杂的账户</p>
    <div class="cards3"><div class="feature"><div class="num">01</div><h3>选择公告</h3><p>填写邮箱，勾选计算机本科、计算机大学院、国际处中你关心的来源。</p></div>
    <div class="feature"><div class="num">02</div><h3>确认邮箱</h3><p>点击确认邮件里的按钮，订阅立即生效。之后可以设置密码，随时登录管理。</p></div>
    <div class="feature"><div class="num">03</div><h3>自动接收</h3><p>重要公告即时送达，普通公告工作日 {hours} 汇总，截止日期前还会提醒。</p></div></div>

    <h2 class="section-title">为留学生设计</h2><p class="section-sub">读懂韩文公告，不再靠翻译软件逐句猜</p>
    <div class="cards4"><div class="feature">{icon("bolt")}<h3>重要事项即时提醒</h3><p>签证、奖学金、毕业、选课等公告分析完成后立即发送，周末也不例外。</p></div>
    <div class="feature">{icon("clock")}<h3>截止日期提醒</h3><p>自动识别申请截止时间，在 D-7、D-3、D-1 和当天提醒你。</p></div>
    <div class="feature">{icon("text")}<h3>准确的中文摘要</h3><p>说明这是什么、谁需要关注、要做什么，并保留原文日期和学校原公告链接。</p></div>
    <div class="feature">{icon("shield")}<h3>只收集邮箱</h3><p>不需要姓名、学号或手机号。每封邮件都能一键退订，也可以随时注销。</p></div></div>

    {"" if view.account else '<div class="cta"><div><h2>下一条重要公告，直接送到你的邮箱</h2><p>免费订阅，一分钟完成。</p></div><a class="btn" href="/subscribe" style="background:#0a2a57;color:#fff">免费订阅</a></div>'}"""

def steps(current: int) -> str:
    """Signup is three steps; showing them makes it clear that submitting the form is not the end."""
    labels = ("填写邮箱和订阅内容", "去邮箱点击确认链接", "订阅完成")
    items = "".join(
        f'<li class="{"done" if index < current else "on" if index == current else ""}"><b>第 {index} 步</b>{label}</li>'
        for index, label in enumerate(labels, 1))
    return f'<ol class="steps">{items}</ol>'


def turnstile_widget(view: View) -> str:
    site_key = view.settings.turnstile_site_key if view.settings.turnstile_secret else None
    if not site_key:
        return ""
    return (f'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
            f'<div class="field cf-turnstile" data-sitekey="{html.escape(site_key)}" data-action="{TURNSTILE_ACTION}"></div>')


WEBMAIL = {"gmail.com": ("Gmail", "https://mail.google.com/"), "naver.com": ("Naver 邮箱", "https://mail.naver.com/"),
           "daum.net": ("Daum 邮箱", "https://mail.daum.net/"), "hanmail.net": ("Daum 邮箱", "https://mail.daum.net/"),
           "outlook.com": ("Outlook", "https://outlook.live.com/mail/"), "hotmail.com": ("Outlook", "https://outlook.live.com/mail/"),
           "qq.com": ("QQ 邮箱", "https://mail.qq.com/"), "163.com": ("163 邮箱", "https://mail.163.com/"),
           "126.com": ("126 邮箱", "https://mail.126.com/")}


def subscribe_page(view: View, message: str = "", error: bool = False) -> str:
    return (f'{steps(1)}<h1>订阅公告</h1><p class="sub">填写邮箱并选择想接收的公告。提交后需要到邮箱里点击确认链接，订阅才会生效。'
            f'已经订阅过？<a href="/login">登录</a></p>{alert(message, error)}') + f"""
    <div class="card"><form method="post" action="/subscribe">
    <div class="field"><label>邮箱地址</label><input type="email" name="email" required autocomplete="email" placeholder="student@pusan.ac.kr"></div>
    {preference_fields(view.settings, {s.key for s in SOURCES}, True, True, True)}
    {turnstile_widget(view)}<button class="btn" type="submit">下一步：去邮箱确认</button></form></div>"""


# Served as a file because the CSP forbids inline scripts. Polls until the confirmation link is clicked, which is
# often in another browser (a mail app's built-in one), then moves this page on to the “done” step.
CHECK_EMAIL_JS = """(function () {
  var box = document.getElementById('confirm-watch');
  if (!box) return;
  var token = encodeURIComponent(box.getAttribute('data-token')), started = Date.now(), busy = false;
  function check() {
    if (busy || Date.now() - started > 30 * 60 * 1000) return;
    busy = true;
    fetch('/subscribe/status?t=' + token, {cache: 'no-store', credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { if (d.confirmed) location.href = '/subscribe/done?t=' + token; })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  setInterval(check, 4000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) check(); });
  window.addEventListener('focus', check);
})();
"""


def check_email_page(view: View, email: str, preferences: tuple[list[str], bool, bool, bool], message: str = "",
                     error: bool = False, poll_token: str = "") -> str:
    sources, immediate, daily, weekly = preferences
    domain = email.rsplit("@", 1)[-1].casefold()
    webmail = WEBMAIL.get(domain)
    open_mail = (f'<a class="btn" href="{webmail[1]}" target="_blank" rel="noopener">打开 {webmail[0]}</a>' if webmail else "")
    # Resending posts the same choices again; the service enforces the cooldown between emails.
    hidden = (f'<input type="hidden" name="email" value="{html.escape(email)}"><input type="hidden" name="resend" value="1">'
              + "".join(f'<input type="hidden" name="source" value="{html.escape(key)}">' for key in sources)
              + "".join(f'<input type="hidden" name="{name}" value="on">'
                        for name, on in (("immediate", immediate), ("daily", daily), ("weekly", weekly)) if on))
    watch = (f'<div id="confirm-watch" data-token="{html.escape(poll_token)}"></div>'
             '<script src="/static/check-email.js" defer></script>' if poll_token else "")
    done_link = (f'<p class="hint">已经点过确认链接了？<a href="/subscribe/done?t={html.escape(poll_token)}">查看订阅状态</a></p>'
                 if poll_token else "")
    return f"""{steps(2)}<h1>还差一步：去邮箱确认</h1>{alert(message, error)}
    {watch}<div class="card"><p class="big">确认邮件已发送到 <strong>{html.escape(email)}</strong>。<br>
    请打开邮件，点击里面的<strong>「确认订阅」</strong>按钮。<strong>在确认之前，订阅不会生效，也不会收到任何公告。</strong></p>
    <div class="row">{open_mail}</div>
    <ul class="checklist"><li>发件人是 {html.escape(view.settings.from_name)} &lt;{html.escape(view.settings.from_email)}&gt;，
    标题是“还差一步：确认你的 PNU Notice 订阅”。</li>
    <li>几分钟内没收到的话，请检查<strong>垃圾邮件</strong>或<strong>推广邮件</strong>文件夹。</li>
    <li>确认链接 24 小时内有效。在手机或其他浏览器里点也可以，这个页面会自动更新。</li></ul>{done_link}</div>
    <div class="card"><h2>没收到邮件？</h2><p class="sub">可以重新发送（每 5 分钟最多一次），或者检查邮箱是否填错。</p>
    <form method="post" action="/subscribe">{hidden}{turnstile_widget(view)}<div class="row">
    <button class="btn secondary">重新发送确认邮件</button><a href="/subscribe">邮箱填错了，重新填写</a></div></form></div>"""


def email_manage_page(view: View, token: str, subscriber: dict, subscriptions: list[dict], message: str = "") -> str:
    hidden = f'<input type="hidden" name="token" value="{html.escape(token)}">'
    return f"""<h1>管理订阅</h1><p class="sub">你正在通过邮件中的链接管理订阅，无需登录。想以后直接登录管理？
    先<a href="/forgot-password">设置密码</a>。</p>{alert(message)}
    {subscription_cards(view, subscriber, subscriptions, "/subscription/manage", hidden)}"""


def login_page(message: str = "", error: bool = False) -> str:
    return f"""<h1>登录</h1><p class="sub">用订阅时验证过的邮箱登录。还没有订阅？<a href="/subscribe">订阅公告</a></p>{alert(message, error)}
    <div class="card"><form method="post" action="/login">
    <div class="field"><label>邮箱地址</label><input type="email" name="email" required autocomplete="email"></div>
    <div class="field"><label>密码</label><input type="password" name="password" required autocomplete="current-password"></div>
    <div class="row"><button class="btn">登录</button><a href="/forgot-password">忘记密码 / 还没设置过密码</a></div></form></div>"""


def done_page(view: View, email: str) -> str:
    return f"""{steps(3)}<h1>订阅已生效</h1><div class="card"><p class="big">公告会发送到 <strong>{html.escape(email)}</strong>。</p>
    <p class="muted">你是在另一个浏览器（例如邮箱 App 里）完成确认的，那边已经自动登录，可以直接在那里设置密码。
    想在这个浏览器里管理订阅，设置一个密码后登录即可。</p>
    <div class="row"><a class="btn" href="/forgot-password?email={html.escape(email)}">在这里设置密码</a><a href="/">返回首页</a></div>
    <p class="hint">不设密码也没关系：每封公告邮件底部都有“管理订阅”和“退订”链接。</p></div>"""


def forgot_page(message: str = "", error: bool = False, email: str = "") -> str:
    return f"""<h1>设置或重置密码</h1><p class="sub">输入订阅时验证过的邮箱，我们会发送一个设置密码的链接。</p>{alert(message, error)}
    <div class="card"><form method="post" action="/forgot-password">
    <div class="field"><label>邮箱地址</label><input type="email" name="email" required autocomplete="email" value="{html.escape(email)}"></div>
    <button class="btn">发送链接</button></form>
    <p class="hint" style="margin-top:18px">没收到邮件：请检查垃圾邮件箱；每 5 分钟最多发送一次；只有点过确认链接的订阅邮箱才能设置密码。<br>
    管理员账户出于安全原因不能通过邮件重置，请在服务器上执行 <code>pnu-notice set-admin-password</code>。</p></div>"""


def reset_page(token: str, message: str = "") -> str:
    return f"""<h1>设置新密码</h1><p class="sub">设置完成后会自动登录。</p>{alert(message, True)}
    <div class="card"><form method="post" action="/reset-password"><input type="hidden" name="token" value="{html.escape(token)}">
    {new_password_fields()}<button class="btn">保存密码</button></form></div>"""


def message_page(title: str, text: str) -> str:
    return f'<h1>{html.escape(title)}</h1><div class="card"><p class="muted">{html.escape(text)}</p><a class="btn secondary" href="/">返回首页</a></div>'


PRIVACY = """<h1>隐私与账户说明</h1><div class="card">
<h2>我们保存什么</h2><ul class="muted">
<li>邮箱地址，以及你选择的公告来源和接收方式。</li>
<li>如果你设置了密码：密码经过加盐的 scrypt 单向加密后保存，任何人（包括管理员）都看不到原密码。</li>
<li>登录状态（30 天有效，退出或修改密码后失效）和发信记录，用于防止重复发送和排查问题。</li></ul>
<p class="muted">我们不收集姓名、学号、手机号、国籍等信息，也不会把你的邮箱提供给任何第三方用于营销。邮件通过 Resend 发送，网站经由 Cloudflare 提供访问。</p>
<h2>订阅与登录</h2><ul class="muted">
<li>订阅需要先点击验证邮件中的链接，未验证的邮箱不会收到任何公告邮件。</li>
<li>验证后可以设置密码，用邮箱和密码登录管理订阅。忘记密码可以通过邮箱重新设置。</li>
<li>连续多次输错密码，账户会被暂时锁定 15 分钟。</li></ul>
<h2>退订</h2><p class="muted">每封邮件底部都有“退订”链接，无需登录即可退订。退订后不再发送公告邮件；为防止重复发送，退订记录会被保留。</p>
<h2>注销账户</h2><p class="muted">登录后可以在“我的订阅”页面底部注销账户。注销会永久删除你的邮箱、订阅设置、密码和发信记录，无法恢复。</p>
<h2>内容说明</h2><p class="muted">邮件中的中文摘要由 AI 根据釜山大学官方公告自动整理，仅用于辅助理解，具体要求请以学校原公告及附件为准。</p></div>"""


class Request:
    def __init__(self, environ):
        self.environ = environ
        self.path = environ.get("PATH_INFO", "/")
        self.method = environ.get("REQUEST_METHOD", "GET")
        self.query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        self._form = None
        cookie = SimpleCookie()
        try:
            cookie.load(environ.get("HTTP_COOKIE", ""))
        except Exception:
            pass
        self.session_token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

    @property
    def form(self) -> dict[str, list[str]]:
        if self._form is None:
            length = min(int(self.environ.get("CONTENT_LENGTH") or 0), 65536)
            self._form = parse_qs(self.environ["wsgi.input"].read(length).decode("utf-8"), keep_blank_values=True)
        return self._form

    def arg(self, name: str) -> str:
        return self.query.get(name, [""])[0]

    def field(self, name: str) -> str:
        return self.form.get(name, [""])[0]

    def preferences(self) -> tuple[list[str], bool, bool, bool]:
        return self.form.get("source", []), "immediate" in self.form, "daily" in self.form, "weekly" in self.form


class WebApp:
    def __init__(self, subscriptions: SubscriptionService, accounts: AccountService | None = None):
        self.subscriptions = subscriptions
        self.settings = subscriptions.settings
        self.accounts = accounts or AccountService(subscriptions.db, subscriptions.settings, subscriptions.mailer)
        self.secure_cookie = subscriptions.settings.app_base_url.startswith("https://")

    @staticmethod
    def _response(start_response, content: str, status: HTTPStatus = HTTPStatus.OK, headers: list | None = None,
                  content_type: str = "text/html; charset=utf-8"):
        data = content.encode("utf-8")
        start_response(f"{status.value} {status.phrase}", [
            ("Content-Type", content_type), ("Content-Length", str(len(data))),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
            ("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"),
            ("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
             "script-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; "
             "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
            *(headers or []),
        ])
        return [data]

    def _page(self, start_response, view: View, content: str, title: str, status: HTTPStatus = HTTPStatus.OK,
              wide: bool = False):
        return self._response(start_response, layout(view, content, title, wide), status)

    def _session_cookie(self, token: str | None) -> tuple[str, str]:
        secure = "; Secure" if self.secure_cookie else ""
        if token is None:
            return "Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
        return "Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax{secure}"

    def _redirect(self, start_response, location: str, session: str | None | bool = False):
        headers = [("Location", location)]
        if session is not False:
            headers.append(self._session_cookie(session))
        return self._response(start_response, "", HTTPStatus.SEE_OTHER, headers)

    def __call__(self, environ, start_response):
        request = Request(environ)
        view = View(self.settings)
        try:
            return self._route(request, start_response, view)
        except ValueError as exc:
            return self._page(start_response, view, message_page("无法完成", str(exc)), "无法完成", HTTPStatus.BAD_REQUEST)
        except Exception:
            traceback.print_exc()
            return self._page(start_response, view, message_page("暂时无法完成", "请稍后重试。"), "暂时无法完成",
                              HTTPStatus.INTERNAL_SERVER_ERROR)

    def _route(self, request: Request, start_response, view: View):
        path, method = request.path, request.method
        if path == "/healthz" and method == "GET":
            with self.subscriptions.db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return self._response(start_response, "ok")
        view.account = self.accounts.session_account(request.session_token)
        view.csrf = self.accounts.csrf_token(request.session_token) if view.account else None
        if method == "POST" and view.account and (path in CSRF_PROTECTED or path.startswith("/admin/")):
            if not self.accounts.check_csrf(request.session_token, request.field("csrf")):
                return self._page(start_response, view, message_page("页面已过期", "请刷新页面后重试。"), "页面已过期",
                                  HTTPStatus.FORBIDDEN)
        if path == "/" and method == "GET":
            return self._page(start_response, view, home_page(view, bool(request.arg("deleted")), self._site_stats()),
                              "PNU Notice", wide=True)
        if path == "/static/admin.js" and method == "GET":
            return self._response(start_response, admin.ADMIN_JS, content_type="text/javascript; charset=utf-8")
        if path == "/static/check-email.js" and method == "GET":
            return self._response(start_response, CHECK_EMAIL_JS, content_type="text/javascript; charset=utf-8")
        if path == "/privacy" and method == "GET":
            return self._page(start_response, view, PRIVACY, "隐私与账户说明")
        if path in ("/subscribe", "/subscribe/status", "/subscribe/done", "/verify", "/subscription/manage", "/unsubscribe"):
            return self._subscription_routes(request, start_response, view)
        if path in ("/login", "/logout", "/forgot-password", "/reset-password"):
            return self._auth_routes(request, start_response, view)
        if path.startswith("/account"):
            if not view.account:
                return self._redirect(start_response, "/login")
            return self._account_routes(request, start_response, view)
        if path.startswith("/admin"):
            if not view.account or view.account["role"] != "admin":
                return self._redirect(start_response, "/login")
            if view.account["must_change_password"]:
                return self._redirect(start_response, "/account")
            return self._admin_routes(request, start_response, view)
        return self._not_found(start_response, view)

    def _site_stats(self) -> dict:
        """Live numbers for the home page, so visitors can see the service is actually running."""
        with self.subscriptions.db.connect() as conn:
            notices = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
            checked = conn.execute("SELECT MAX(started_at) FROM crawl_runs").fetchone()[0]
        return {"notices": notices, "checked": admin.ago(checked) if checked else ""}

    def _not_found(self, start_response, view: View):
        return self._page(start_response, view, message_page("页面不存在", "请检查链接是否完整。"), "页面不存在",
                          HTTPStatus.NOT_FOUND)

    def _own_subscription(self, view: View, subscriber_id: int) -> bool:
        return bool(view.account and view.account["subscriber_id"] == subscriber_id)

    def _subscription_routes(self, request: Request, start_response, view: View):
        path, method = request.path, request.method
        if path == "/subscribe" and view.account and view.account["subscriber_id"]:
            return self._redirect(start_response, "/account")
        if path == "/subscribe" and method == "GET":
            return self._page(start_response, view, subscribe_page(view), "订阅公告")
        if path == "/subscribe" and method == "POST":
            # Cloudflare Tunnel forwards the visitor address in CF-Connecting-IP.
            email, preferences = request.field("email").strip(), request.preferences()
            resend = bool(request.field("resend"))

            def failed(message: str, status: HTTPStatus):
                # A failed resend stays on the “check your email” page instead of bouncing back to the empty form.
                content = (check_email_page(view, email, preferences, message, True) if resend
                           else subscribe_page(view, message, True))
                return self._page(start_response, view, content, "去邮箱确认" if resend else "订阅公告", status)

            if self.settings.turnstile_secret and not verify_turnstile(
                    request.field("cf-turnstile-response"), self.settings.turnstile_secret, TURNSTILE_ACTION,
                    self.settings.turnstile_hostnames, request.environ.get("HTTP_CF_CONNECTING_IP")):
                return failed("人机验证未通过，请重试。", HTTPStatus.FORBIDDEN)
            try:
                poll_token = self.subscriptions.subscribe(email, *preferences)
            except ValueError as exc:
                return failed(str(exc), HTTPStatus.BAD_REQUEST)
            return self._page(start_response, view, check_email_page(
                view, email, preferences, "已重新发送确认邮件。" if resend else "", poll_token=poll_token), "去邮箱确认")
        if path == "/subscribe/status" and method == "GET":
            status = self.subscriptions.confirmation_status(request.arg("t"))
            body = json.dumps({"confirmed": bool(status and status["confirmed"])})
            return self._response(start_response, body, HTTPStatus.OK if status else HTTPStatus.NOT_FOUND,
                                  content_type="application/json")
        if path == "/subscribe/done" and method == "GET":
            status = self.subscriptions.confirmation_status(request.arg("t"))
            if not status:
                return self._not_found(start_response, view)
            if self._own_subscription(view, status["subscriber_id"]):
                return self._redirect(start_response, "/account")
            if not status["confirmed"]:
                return self._page(start_response, view, message_page(
                    "还没有完成确认", "我们还没收到确认。请打开确认邮件，点击里面的「确认订阅」按钮，然后回到这里刷新页面。"),
                    "还没有完成确认")
            return self._page(start_response, view, done_page(view, status["email"]), "订阅已生效")
        if path == "/verify" and method == "GET":
            # Clicking the link proves mailbox ownership, so it also signs the user in to set a password.
            manage = self.subscriptions.verify(request.arg("token"))
            subscriber, _ = self.subscriptions.get_by_management_token(manage)
            account = self.accounts.ensure_for_subscriber(subscriber)
            return self._redirect(start_response, "/account?welcome=1", self.accounts.create_session(account))
        if path == "/subscription/manage" and method == "GET":
            token = request.arg("token")
            subscriber, subscriptions = self.subscriptions.get_by_management_token(token)
            if self._own_subscription(view, subscriber["id"]):
                return self._redirect(start_response, "/account")
            return self._page(start_response, view, email_manage_page(view, token, subscriber, subscriptions), "管理订阅")
        if path == "/subscription/manage" and method == "POST":
            token, action = request.field("token"), request.field("action") or "save"
            self.subscriptions.update(token, *request.preferences(), action)
            subscriber, subscriptions = self.subscriptions.get_by_management_token(token)
            return self._page(start_response, view, email_manage_page(
                view, token, subscriber, subscriptions, ACTION_MESSAGES.get(action, "")), "管理订阅")
        if path == "/unsubscribe" and method == "GET":
            token = request.arg("token")
            subscriber, _ = self.subscriptions.get_by_management_token(token)
            return self._page(start_response, view, f"""<h1>退订</h1><div class="card">
            <p class="muted">确认停止向 {html.escape(subscriber['email'])} 发送所有公告邮件？如果只是想少收一些，
            可以<a href="/subscription/manage?token={html.escape(token)}">调整订阅设置</a>。</p>
            <form method="post" action="/subscription/manage"><input type="hidden" name="token" value="{html.escape(token)}">
            <button class="btn danger" name="action" value="unsubscribe">确认退订</button></form></div>""", "退订")
        return self._not_found(start_response, view)

    def _auth_routes(self, request: Request, start_response, view: View):
        path, method = request.path, request.method
        if path == "/login" and method == "GET":
            if view.account:
                return self._redirect(start_response, "/admin" if view.account["role"] == "admin" else "/account")
            return self._page(start_response, view, login_page(), "登录")
        if path == "/login" and method == "POST":
            try:
                account = self.accounts.authenticate(request.field("email"), request.field("password"))
            except ValueError as exc:
                return self._page(start_response, view, login_page(str(exc), True), "登录", HTTPStatus.UNAUTHORIZED)
            target = "/admin" if account["role"] == "admin" and not account["must_change_password"] else "/account"
            return self._redirect(start_response, target, self.accounts.create_session(account))
        if path == "/logout" and method == "POST":
            if request.session_token:
                self.accounts.logout(request.session_token)
            return self._redirect(start_response, "/", None)
        if path == "/forgot-password" and method == "GET":
            return self._page(start_response, view, forgot_page(email=request.arg("email")), "设置或重置密码")
        if path == "/forgot-password" and method == "POST":
            self.accounts.request_password_reset(request.field("email"))
            return self._page(start_response, view, forgot_page(
                "已提交。如果这是一个已确认订阅的普通用户邮箱，设置密码的邮件会在几分钟内送达。"), "设置或重置密码")
        if path == "/reset-password" and method == "GET":
            token = request.arg("token")
            if not self.accounts.reset_token_valid(token):
                return self._page(start_response, view, forgot_page("链接无效、已使用或已过期，请重新申请。", True),
                                  "设置或重置密码", HTTPStatus.BAD_REQUEST)
            return self._page(start_response, view, reset_page(token), "设置新密码")
        if path == "/reset-password" and method == "POST":
            token = request.field("token")
            if request.field("new_password") != request.field("confirm_password"):
                return self._page(start_response, view, reset_page(token, "两次输入的密码不一致。"), "设置新密码",
                                  HTTPStatus.BAD_REQUEST)
            try:
                session = self.accounts.reset_password(token, request.field("new_password"))
            except ValueError as exc:
                return self._page(start_response, view, reset_page(token, str(exc)), "设置新密码", HTTPStatus.BAD_REQUEST)
            return self._redirect(start_response, "/account?password=1", session)
        return self._not_found(start_response, view)

    def _account_page(self, view: View, message: str = "", error: bool = False) -> str:
        account = view.account
        if account["must_change_password"]:
            title, note = "修改初始密码", '<div class="alert">这是初始密码，请先修改后再使用管理后台。</div>'
        elif account["password_hash"]:
            title, note = "修改密码", ""
        else:
            title, note = "设置登录密码", '<p class="sub">设置后就可以随时用邮箱和密码登录，管理你的订阅。</p>'
        current = ('<div class="field"><label>当前密码</label><input type="password" name="current_password" required '
                   'autocomplete="current-password"></div>' if account["password_hash"] else "")
        password_card = f"""<div class="card"><h2>{title}</h2>{note}
        <form method="post" action="/account/password">{view.hidden_csrf()}{current}{new_password_fields()}
        <button class="btn">保存密码</button></form></div>"""
        if account["must_change_password"]:
            return f'<h1>我的账户</h1><p class="sub">{html.escape(account["email"])}</p>{alert(message, error)}{password_card}'
        if account["subscriber_id"]:
            subscriber, subscriptions = self.subscriptions.get(account["subscriber_id"])
            subscription = subscription_cards(view, subscriber, subscriptions, "/account", view.hidden_csrf(), False)
        else:
            subscription = ('<div class="card"><h2>订阅设置</h2><p class="sub">这个账户还没有订阅公告。</p>'
                            '<a class="btn" href="/subscribe">订阅公告</a></div>')
        # A brand-new account sees the password prompt first; afterwards it sits below the subscription settings.
        cards = password_card + subscription if not account["password_hash"] else subscription + password_card
        if account["role"] != "admin":
            if account["password_hash"]:
                proof = ('<div class="field"><label>输入当前密码确认</label><input type="password" name="current_password" '
                         'required autocomplete="current-password"></div>')
            else:
                proof = (f'<div class="field"><label>输入你的邮箱地址确认</label><input type="email" name="confirm_email" required '
                         f'autocomplete="off" placeholder="{html.escape(account["email"])}"></div>')
            cards += ('<div class="card"><h2>注销账户</h2><p class="sub">永久删除你的邮箱、订阅设置、密码和所有发信记录，'
                      '之后不会再收到任何邮件。此操作<strong>无法恢复</strong>。只是暂时不想收邮件的话，可以用上面的“暂停订阅”。</p>') + f"""
            <form method="post" action="/account/delete">{view.hidden_csrf()}{proof}
            <button class="btn danger">永久注销我的账户</button></form></div>"""
        return f'<h1>我的订阅</h1><p class="sub">{html.escape(account["email"])}</p>{alert(message, error)}{cards}'

    def _account_routes(self, request: Request, start_response, view: View):
        path, method = request.path, request.method
        account = view.account
        if path == "/account" and method == "GET":
            message = ("邮箱验证成功，订阅已启用。设置一个密码，以后就能直接登录。" if request.arg("welcome")
                       else "密码已保存。" if request.arg("password") else "")
            welcome = steps(3) if request.arg("welcome") else ""
            return self._page(start_response, view, welcome + self._account_page(view, message), "我的订阅")
        if path == "/account" and method == "POST" and account["subscriber_id"] and not account["must_change_password"]:
            action = request.field("action") or "save"
            try:
                self.subscriptions.update_subscriber(account["subscriber_id"], *request.preferences(), action)
            except ValueError as exc:
                return self._page(start_response, view, self._account_page(view, str(exc), True), "我的订阅",
                                  HTTPStatus.BAD_REQUEST)
            return self._page(start_response, view, self._account_page(view, ACTION_MESSAGES.get(action, "")), "我的订阅")
        if path == "/account/delete" and method == "POST":
            if account["role"] == "admin":
                return self._page(start_response, view, self._account_page(view, "管理员账户不能注销。", True), "我的订阅",
                                  HTTPStatus.BAD_REQUEST)
            # Proof of intent: the password, or for password-less accounts the full email address typed out.
            if account["password_hash"]:
                confirmed = check_password(request.field("current_password"), account["password_hash"])
                failure = "密码不正确，账户没有注销。"
            else:
                confirmed = request.field("confirm_email").strip().casefold() == account["email_normalized"]
                failure = "输入的邮箱与账户不一致，账户没有注销。"
            if not confirmed:
                return self._page(start_response, view, self._account_page(view, failure, True), "我的订阅",
                                  HTTPStatus.BAD_REQUEST)
            self.accounts.delete_person(account_id=account["id"])
            return self._redirect(start_response, "/?deleted=1", None)
        if path == "/account/password" and method == "POST":
            if request.field("new_password") != request.field("confirm_password"):
                return self._page(start_response, view, self._account_page(view, "两次输入的新密码不一致。", True), "我的订阅",
                                  HTTPStatus.BAD_REQUEST)
            try:
                session = self.accounts.change_password(account, request.field("new_password"),
                                                        request.field("current_password"))
            except ValueError as exc:
                return self._page(start_response, view, self._account_page(view, str(exc), True), "我的订阅",
                                  HTTPStatus.BAD_REQUEST)
            return self._redirect(start_response, "/account?password=1", session)
        return self._not_found(start_response, view)

    def _admin_routes(self, request: Request, start_response, view: View):
        path, method = request.path, request.method
        db = self.subscriptions.db
        args = {key: values[0] for key, values in request.query.items()}
        message = ""
        detail = re.fullmatch(r"/admin/notices/(\d+)", path)
        if path == "/admin/subscribers" and method == "POST":
            action = request.field("action")
            if action in ("pause", "resume", "unsubscribe"):
                self.subscriptions.update_subscriber(int(request.field("id")), [], False, False, False, action)
                message = {"pause": "已暂停该订阅。", "resume": "已恢复该订阅。", "unsubscribe": "已替该用户退订。"}[action]
            elif action == "delete":
                try:
                    email = self.accounts.delete_person(subscriber_id=int(request.field("id")))
                    message = f"已永久删除 {email} 的所有数据。"
                except ValueError as exc:
                    message = str(exc)
        if detail and method == "POST" and request.field("action") == "reanalyze":
            admin.reanalyze(db, int(detail.group(1)))
            message = "已加入 AI 分析队列，一两分钟后刷新即可看到新结果。"
        if detail:
            page, active = admin.notice_detail(db, view.csrf, int(detail.group(1))), "/admin/notices"
            if page is None:
                return self._not_found(start_response, view)
        elif path == "/admin":
            page, active = admin.overview(db, self.settings), path
        elif path == "/admin/subscribers":
            page, active = admin.subscribers_page(db, view.csrf, args), path
        elif path == "/admin/notices":
            page, active = admin.notices_page(db, args), path
        else:
            return self._not_found(start_response, view)
        title, subtitle, content = page
        return self._response(start_response, admin.shell(view.account["email"], view.csrf, active, title, subtitle,
                                                          content, message))


def create_app() -> WebApp:
    from .cli import services

    settings, db, crawler, ai_worker, planner, delivery_worker, mailer = services()
    return WebApp(SubscriptionService(db, settings, mailer))
