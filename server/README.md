# 服务器管理（server/ctl.sh）

所有服务器操作都通过 `server/ctl.sh` 完成，服务器信息写在配置文件里，脚本本身不用改。

```bash
cp server/server.conf.example server/server.conf   # 填 HOST、DIR、URL、MODE
server/ctl.sh check                                 # 只读检查
server/ctl.sh deploy                                # 日常更新
```

`server/*.conf` 不进 git（`server.conf.example` 除外）。有多台服务器时，每台写一个配置文件，用 `-c` 指定：`server/ctl.sh -c server/tokyo.conf status`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `check` | 只读：ssh、Docker 版本、swarm 状态、`.env`/隧道凭证/数据卷是否就绪、服务器能否访问学校网站、公网 `/healthz` |
| `setup [--from OLD.conf] [--env FILE] [--cloudflared DIR]` | 初始化新服务器：建目录、复制 `.env` 和隧道凭证、把 `HOST_UID/HOST_GID` 改成新服务器的用户、建数据卷、上传代码。不启动服务，已有文件不覆盖 |
| `deploy [--skip-tests] [--allow-dirty] [--dry-run] [服务...]` | 跑测试 → 上传 → 构建 → 重启 → 检查 `/healthz` 和日志 |
| `migrate --from OLD.conf [--force] [--yes]` | 先在新服务器上构建，再停旧服务器，把整个数据卷搬过来（本地留一份 `server/backups/migrate-*.tar.gz`），然后启动新服务器 |
| `start` / `stop` / `status` | 启动、停止、查看状态。`stop` 不删除数据库 |
| `logs [服务] [-f]` | 查看日志，默认 scheduler |
| `rollback` | 仅 swarm：所有服务回到上一个镜像 |
| `swarm-init [--yes]` | 把服务器的 Docker 切换成单节点 swarm。不加 `--yes` 只显示会执行什么 |
| `switch compose\|swarm` | 换模式运行，并同步修改配置里的 `MODE` |

## 换服务器

```bash
cp server/server.conf server/lab.conf               # 旧服务器
cp server/server.conf.example server/new.conf       # 新服务器：改 HOST 等
server/ctl.sh -c server/new.conf setup --from server/lab.conf
server/ctl.sh -c server/new.conf check              # 重点看 http 200（学校网站会挡部分机器）
server/ctl.sh -c server/new.conf migrate --from server/lab.conf
cp server/new.conf server/server.conf               # 以后默认操作新服务器
```

新旧服务器共用同一个 Cloudflare Tunnel，域名不用改。迁移完成后旧服务器处于停止状态，数据卷原样保留。如果新服务器有问题，先对新服务器执行 `stop`，再对旧服务器执行 `start`。两边绝不能同时运行，否则 scheduler 会把每封邮件发两遍。`deploy`、`start` 和 `migrate` 都会检查这一点。

## compose 与 swarm

两种模式使用同一个数据卷 `pnu-notice_pnu_data`，切换模式不会影响数据库。数据库是这一个卷里的 SQLite 文件，容器重启不会动它。

| | compose（`compose.tunnel.yaml`） | swarm（`compose.swarm.yaml`） |
| --- | --- | --- |
| 进程崩溃 / 服务器重启 | 自动重启 | 自动重启 |
| 健康检查失败 | 只标记 unhealthy，不重启 | 自动替换容器 |
| 更新 | 停旧容器再起新容器，有几秒中断 | web 先起新容器，健康后再停旧的；新版本起不来时自动回滚 |
| 回滚 | 检出旧提交后重新 deploy | `rollback`，保留最近 `KEEP_IMAGES` 个镜像 |
| 限制 | — | 不支持 `security_opt`（`no-new-privileges` 失效，`cap_drop: ALL` 仍然有效）；`swarm-init` 作用于整个 Docker 守护进程 |

切换到 swarm：

```bash
server/ctl.sh swarm-init          # 先看说明
server/ctl.sh swarm-init --yes
server/ctl.sh switch swarm        # 大约一分钟中断
```

切回 compose：`server/ctl.sh switch compose`。不再使用 swarm 时，可以在服务器上执行 `docker swarm leave --force`。
