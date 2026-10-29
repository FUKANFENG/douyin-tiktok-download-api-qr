# Docker 安装与维护

本指南适用于本仓库的扫码增强版。上游原始文档保留作参考；本版使用根目录 `compose.qr.yaml` 和独立的 `.env.qr`，默认项目名 `dtk-qr`。

## 组件与数据

| 服务 | 作用 | 持久数据 |
|---|---|---|
| api | 登录、控制台、API、扫码桥接 | 读取媒体，管理应用备份 |
| worker | 解析、后台任务、提醒 | 应用备份 |
| migrate | 首次启动及升级时应用数据库迁移 | 一次性任务，成功后退出 |
| postgres | TimescaleDB / PostgreSQL 17 | `postgres-data` |
| redis | 会话、队列、缓存 | `redis-data` |
| downloader | 下载媒体文件 | `media-data` |
| qr-login | 后台 Chromium 扫码及加密存储 | `qr-data` |

只有控制台端口默认映射到宿主机 `127.0.0.1:8000`。扫码服务、数据库和 Redis 不映射宿主端口。扫码服务在 Docker 模式下只接受后台 Bearer 认证，不开放独立网页领取凭证。

镜像构建会下载 Node、Python、Chromium 和锁定的 Python / npm 依赖。Docker 需要访问 Docker Hub、PyPI、npm 和 Playwright 下载站。安装后不依赖宿主机 Python、Node、Chrome 或 Edge。

## Windows / Docker Desktop

需要 PowerShell 7 和 Docker Desktop 的 Linux 容器模式。在仓库根目录运行：

```powershell
.\Install-QR.ps1
```

脚本在 Docker 中生成随机密钥，保存到 `.env.qr`，构建镜像、启动服务并等待健康检查。新实例会输出一次性管理员初始化链接；打开它设置自己的账号与密码，没有共享的默认密码。

更换端口或项目名，仅在首次生成配置时使用：

```powershell
.\Install-QR.ps1 -Port 18080 -Project my-dtk-qr
```

已有 `.env.qr` 会原样保留，不会重置账号、密钥或数据。已有配置需要换端口时，修改 `DTK_BIND_PORT` 后运行下方启动命令即可。

## Linux / macOS

在仓库根目录生成配置，文件归当前宿主用户所有：

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/workspace" -w /workspace \
  --entrypoint python evil0ctal/douyin_tiktok_download_api:5.1.7 scripts/init_qr.py
docker compose --env-file .env.qr -f compose.qr.yaml build api qr-login
docker compose --env-file .env.qr -f compose.qr.yaml up -d --no-build --wait
docker compose --env-file .env.qr -f compose.qr.yaml logs api
```

从首次启动日志找到 `/setup?token=...` 初始化链接，打开并创建管理员。自定义映射端口时，将该链接的端口替换为 `.env.qr` 中的 `DTK_BIND_PORT`。目前以 Linux/amd64 为主要验证目标；其他架构需自行验证上游镜像与 Chromium 的可用性。

## 日常操作

```text
docker compose --env-file .env.qr -f compose.qr.yaml ps -a
docker compose --env-file .env.qr -f compose.qr.yaml logs --tail 100 api worker qr-login
docker compose --env-file .env.qr -f compose.qr.yaml stop
docker compose --env-file .env.qr -f compose.qr.yaml up -d --no-build --wait
```

`migrate` 显示 `Exited (0)` 是正常状态。`stop` 和不带 `-v` 的 `down` 保留持久卷。`down -v` 会删除数据卷，不用于日常停止或升级。

## 使用扫码登录

进入“身份池 → 扫码登录抖音 → 获取登录二维码”，用手机抖音扫码并确认。完成后自动加密导入身份池，保留浏览器信息和预计期限。重复导入复用已有身份，重新扫码替换对应旧身份。

刷新后重新打开弹窗，可继续当前标签页未处理的会话。服务重启后可以从已保存账号手动导入。二维码会话默认三分钟，过期或取消时清理后台浏览器。平台要求验证码时，由本人完成验证。

## 备份与更新

至少同时保留：`.env.qr`、数据库、`qr-data`（含加密记录与原始 `vault.key`）、媒体文件。应用内“包含身份”的备份会保存身份和扫码关联；它不替代对扫码存储卷和媒体卷的备份。

更新前先保留完整备份。保持 `.env.qr`、项目名和卷不变，再执行：

```text
docker compose --env-file .env.qr -f compose.qr.yaml build api qr-login
docker compose --env-file .env.qr -f compose.qr.yaml run --rm --no-deps migrate
docker compose --env-file .env.qr -f compose.qr.yaml up -d --no-build --wait
```

不要套用上游 `dtkctl upgrade` 直接替换官方镜像。本版数据库包含额外迁移，源码和迁移必须一起保留。上游自动更新提醒默认关闭，可在系统设置中自行开启。

## 排查

- 扫码服务未就绪：查看 `qr-login` 日志和健康状态。Chromium 需要内核支持用户命名空间及 `docker/qr-seccomp.json`；不要用特权容器替代故障排查。
- 二维码超时：确认扫码容器能访问抖音，取消后重新获取；验证码和平台限制需要本人处理。
- 导入失败：通过弹窗重试；确认 `.env.qr` 的服务令牌一致，且扫码凭证卷与密钥没有分离。
- 端口冲突：已有 8000 端口部署时，为新测试实例设置不同端口和项目名。
- 远程访问：使用 HTTPS 反向代理并配置访问控制。不要把数据库、Redis 或扫码服务端口公开。

参考：[Playwright Docker 文档](https://playwright.dev/python/docs/docker)、[上游安装文档](../zh/02-installation.md)。
