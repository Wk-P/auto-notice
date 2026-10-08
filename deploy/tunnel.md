# 研究室服务器部署（Cloudflare Tunnel）

适用于没有公网 IP 的机器。机器主动连到 Cloudflare，不需要开放任何入站端口，HTTPS 证书由 Cloudflare 提供。

需要：一台装好 Docker Engine 和 Docker Compose v2 的 Linux 机器，以及一个 DNS 托管在 Cloudflare 的域名。

## 1. 创建 Tunnel

1. 打开 Cloudflare 控制台 → Zero Trust → Networks → Tunnels → Create a tunnel，类型选 **Cloudflared**，起个名字（如 `pnu-notice`）。
2. 安装方式页面选 **Docker**，复制命令里 `--token` 后面的那一长串，这就是 `TUNNEL_TOKEN`。不用执行那条命令。
3. 下一步 **Public Hostname**：
   - Subdomain / Domain：例如 `notice` + `example.com`
   - Service：类型 `HTTP`，URL 填 `web:8000`
4. 保存。Cloudflare 会自动添加对应的 DNS 记录。

## 1.5 创建 Turnstile（订阅表单的人机验证）

防止机器人用别人的邮箱批量提交订阅，消耗发信额度、拖累发信域名的信誉。

1. Cloudflare 控制台 → Turnstile → Add widget。
2. Hostname 填正式域名，例如 `notice.example.com`。Widget Mode 选 **Managed**。
3. 创建后复制 **Site Key** 和 **Secret Key**，第 3 步填进 `.env`。

## 2. 上传项目

```bash
rsync -az --exclude .venv --exclude .env --exclude keys --exclude var --exclude backups ./ USER@SERVER:~/pnu-notice/
```

## 3. 设置 .env

```bash
cd ~/pnu-notice
cp .env.production.example .env
openssl rand -hex 32   # 填入 APP_SECRET
chmod 600 .env
```

填写：

- `APP_BASE_URL=https://notice.example.com`（和第 1 步的 Public Hostname 一致）
- `APP_SECRET`：上一步生成的随机值。上线后不要更换，否则邮件里的管理链接全部失效。
- `TUNNEL_TOKEN`：第 1 步复制的 token。
- `OPENAI_API_KEY`、`OPENAI_MODEL`：不填也能运行，邮件会只有原文标题和正文开头。
- `RESEND_API_KEY`、`FROM_EMAIL`、`ADMIN_EMAIL`：`FROM_EMAIL` 的域名必须先在 Resend 验证。
- `TURNSTILE_SITE_KEY`、`TURNSTILE_SECRET`：上面 1.5 步复制的两个值。`TURNSTILE_HOSTNAMES` 填正式域名（如 `notice.example.com`），不要填 localhost。`TURNSTILE_SECRET` 留空则不启用验证。
- `DOMAIN` 只给 Caddy 用，这里可以不填。

## 4. 启动

```bash
docker compose -f compose.tunnel.yaml up -d --build
docker compose -f compose.tunnel.yaml ps
docker compose -f compose.tunnel.yaml logs -f scheduler
```

调度器首次启动会回填 2026-07-01 以来的公告（三个来源合计约 10–20 分钟），历史公告不会发邮件。之后每小时检查一次 RSS（`POLL_INTERVAL_MINUTES`）。

## 5. 验证

```bash
curl -fsS http://127.0.0.1:8000/healthz      # 本机
curl -fsS https://notice.example.com/healthz  # 经由 Cloudflare
```

然后用自己的邮箱走一遍：订阅 → 收到验证邮件 → 点击验证 → 管理页面 → 退订。确认 Resend 后台有发信记录后再公开网址。

## 6. 更新和备份

```bash
cd ~/pnu-notice
docker compose -f compose.tunnel.yaml up -d --build
```

数据库每天备份到 `~/pnu-notice/backups`，保留 14 天。建议再定期把这个目录同步到另一台机器。
