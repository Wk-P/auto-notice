# 单服务器部署

推荐使用一台 Ubuntu 24.04 LTS 服务器、一个指向服务器公网 IPv4 地址的域名，以及 Docker Engine 与 Docker Compose v2。最低配置可从 1 vCPU、1 GB 内存和 10 GB 磁盘开始。

## 1. DNS 和防火墙

在域名服务商添加 `A` 记录，例如把 `notice.example.com` 指向服务器公网 IP。服务器安全组只需开放：

- TCP 22：SSH，最好限制为管理员自己的 IP。
- TCP 80：Caddy 首次签发证书和 HTTP 跳转。
- TCP 443、UDP 443：HTTPS。

端口 8000 不需要向公网开放。

## 2. 上传项目

在服务器创建目录并上传整个项目。使用 Git 时可以执行：

```bash
sudo mkdir -p /opt/pnu-notice
sudo chown "$USER":"$USER" /opt/pnu-notice
git clone YOUR_REPOSITORY_URL /opt/pnu-notice
cd /opt/pnu-notice
```

也可以从本地上传：

```bash
rsync -az --exclude .venv --exclude .env --exclude keys --exclude var ./ USER@SERVER:/opt/pnu-notice/
```

## 3. 设置密钥

```bash
cd /opt/pnu-notice
cp .env.production.example .env
openssl rand -hex 32
chmod 600 .env
```

将生成的随机值填入 `APP_SECRET`，并填写真实的 `DOMAIN`、`APP_BASE_URL`、`OPENAI_API_KEY`、`RESEND_API_KEY`、`FROM_EMAIL` 和 `ADMIN_EMAIL`。`APP_SECRET` 上线后不要随意更换，否则已有验证及订阅管理链接会失效。

Resend 中需要先验证 `FROM_EMAIL` 所属域名。建议为发信子域名配置 SPF、DKIM 和 DMARC。

## 4. 启动

```bash
docker compose -f compose.prod.yaml config
docker compose -f compose.prod.yaml up -d --build
docker compose -f compose.prod.yaml ps
docker compose -f compose.prod.yaml logs --tail=100 web scheduler caddy
```

Caddy 会自动申请和续期 TLS 证书。调度器首次运行会执行历史回填，然后进入每 15 分钟监控。回填进度可查看：

```bash
docker compose -f compose.prod.yaml logs -f scheduler
```

## 5. 验证

```bash
curl -fsS "https://notice.example.com/healthz"
curl -fsS -o /dev/null -w "%{http_code}\n" "https://notice.example.com/subscribe"
```

再使用自己的邮箱完整测试订阅、验证、管理和退订流程。确认 Resend 中的发信记录正常后再公开网址。

## 6. 更新和备份

更新代码：

```bash
cd /opt/pnu-notice
git pull --ff-only
docker compose -f compose.prod.yaml up -d --build
```

`backup` 服务每天使用 SQLite 在线备份 API 创建一致性备份，默认保留 14 天，文件位于服务器的 `/opt/pnu-notice/backups`。手动备份：

```bash
docker compose -f compose.prod.yaml exec scheduler pnu-notice backup --directory /app/var/backups
```

恢复前应停止 `web` 和 `scheduler`，保留当前数据库副本，再用所选备份替换数据卷中的 `pnu_notice.sqlite3`。
