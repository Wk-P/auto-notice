# PNU Notice

面向釜山大学学生的公告抓取、AI 中文分析和邮件订阅服务。第一阶段严格限定为三个官方来源：计算机本科生公告、计算机大学院公告、国际处留学生公告。

系统将抓取、AI 分析和邮件发送拆成可恢复的独立步骤。公告详情及原始 HTML 会先写入 SQLite；OpenAI 或 Resend 暂时不可用时，官方内容不会丢失。所有通知都有数据库唯一键和 Resend 幂等键。

## 本地启动

需要 Python 3.11 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

编辑 `.env`，至少设置：

- `APP_BASE_URL`：邮件链接可访问的 HTTPS 域名。
- `APP_SECRET`：长随机字符串；更改它会撤销已有管理链接和验证链接。
- `OPENAI_API_KEY`：公告结构化分析。
- `RESEND_API_KEY`、`FROM_EMAIL`：验证及公告邮件。发件域名需要先在 Resend 验证。

初始化数据库并启动：

```bash
pnu-notice init-db
pnu-notice serve --host 0.0.0.0 --port 8000
```

另一个进程运行调度器：

```bash
pnu-notice run-scheduler
```

调度器首次启动会从 `2026-07-01 00:00 Asia/Seoul` 回填。RSS 未覆盖该日期时，它会继续读取公告分页，以普通编号公告的整体日期范围作为停止条件。历史公告会分析并建立截止日期，但不会生成逐条历史邮件。完成后每隔 `POLL_INTERVAL_MINUTES` 检查三个 RSS。

也可以分别执行任务，适合 cron、容器任务或故障恢复：

```bash
pnu-notice backfill
pnu-notice poll
pnu-notice process-ai --limit 50
pnu-notice deliver --limit 200
pnu-notice run-cycle
```

使用 Docker Compose 时：

```bash
cp .env.example .env
docker compose up --build
```

部署到单台公网 Linux 服务器时，使用 [生产部署说明](deploy/README.md) 和 `compose.prod.yaml`；没有公网 IP 的机器（如研究室服务器）使用 [Cloudflare Tunnel 部署说明](deploy/tunnel.md) 和 `compose.tunnel.yaml`。生产配置包含 Gunicorn、Caddy 自动 HTTPS、容器健康检查、持久化数据卷和每日数据库备份。

网页地址为 `http://localhost:8000`。公开页面包括 `/`、`/subscribe` 和带令牌的 `/subscription/manage`。

## 数据和运行保证

- 公告唯一键是 `source_key + external_notice_id`，标题只用于展示。
- 内容哈希由标题、清理后的正文和附件清单生成；内容变化写入 `notice_revisions` 并进入重新分析。
- 详情页是事实来源，数据库同时保留 `raw_html` 和 `clean_text`。
- AI 通过 OpenAI Responses API 的严格 JSON Schema 输出，应用层再次检查类型、枚举和日期。失败会按退避重试；多次失败或未配置 `OPENAI_API_KEY` 时，公告以“原标题 + 原文开头”降级进入定时汇总，不会因 AI 不可用而漏发。
- 未验证邮箱处于 `pending`，不能进入公告发送查询。验证令牌随机生成、24 小时过期且只能使用一次。
- 管理令牌由服务密钥签名并保存哈希，可通过重新注册撤销。退订保留历史记录。
- 发信前再次检查 subscriber 状态、来源订阅和相应频率开关。旧任务不会绕过暂停或退订。
- 截止提醒只建立未来的 D-7、D-3、D-1 和 D-Day 任务；发现时已经错过的提醒不会补发。
- 回填完成前发布、却在轮询时才第一次见到的公告（例如回填时详情页抓取失败）按历史公告保存，不会被当作新公告群发。
- 用户关闭了某种频率时自动退到下一种：重要公告在关闭即时通知时进入定时汇总；普通、低优先级公告不会升级为即时邮件。
- 已订阅的邮箱再次提交订阅表单时，原订阅保持不变，新的偏好要等邮箱主人点击验证链接后才生效。
- 429 和 5xx 请求使用指数退避，学校站点默认限制为每秒一次请求。

数据库业务时间以 `Asia/Seoul` 计算，时间戳统一以 UTC ISO 8601 保存。SQLite 使用 WAL 模式；如果以后扩展到多主机部署，应将数据库替换为 PostgreSQL，同时保留当前唯一约束和事务边界。

OpenAI Structured Outputs 的实现遵循[官方文档](https://developers.openai.com/api/docs/guides/structured-outputs)，使用 Responses API 的 `text.format` JSON Schema，并仍在服务端验证返回值。

## 发送时间

- 学校网站每 `POLL_INTERVAL_MINUTES`（默认 15 分钟）检查一次。
- 重要公告（critical / high）分析完立即发送，周末也发。
- 普通公告在工作日 `DIGEST_HOURS`（默认 10:00、13:00、16:00、19:00，首尔时间）合并发送，白天发布的公告通常 3 小时内收到；周末和前一天 19:00 以后的公告在下一个工作日 10:00 发送。
- 低优先级公告每周汇总一次，默认周五 19:00（`WEEKLY_DIGEST_WEEKDAY=4`，`WEEKLY_DIGEST_HOUR=19`）。

## 账户与管理后台

- 用户：订阅并点击验证链接后自动登录，在“我的账户”设置密码；之后用邮箱和密码登录，修改订阅、暂停或退订。忘记密码通过邮件重置（链接 1 小时有效、一次性）。邮件底部的管理和退订链接无需登录。
- 管理员：`/admin` 提供总览、订阅用户和公告三个页面。管理员不能通过邮件重置密码，只能登录后修改，或在服务器上执行：

  ```bash
  docker compose -f compose.tunnel.yaml exec web pnu-notice set-admin-password admin@example.com
  ```

  这条命令也用于创建管理员。设置后下次登录必须先修改密码。
- 密码用加盐 scrypt 保存；连续 5 次输错锁定 15 分钟；会话 Cookie 为 HttpOnly + Secure + SameSite=Lax，登录后的所有表单带 CSRF 校验。
