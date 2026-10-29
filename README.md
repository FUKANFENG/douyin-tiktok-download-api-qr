# DTK QR Edition · 抖音扫码登录 Docker 版

基于 [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) **v5.1.7** 的非官方定制版。保留上游抖音、TikTok、Bilibili API 和下载控制台，增加容器内抖音扫码登录、自动入池与持久化修复。

扫码登录目前支持抖音。安装端只需要 Docker；Windows 快速安装另需 PowerShell 7，不需要安装 Python、Node 或桌面浏览器。

## 快速安装

```powershell
git clone https://github.com/FUKANFENG/douyin-tiktok-download-api-qr.git
cd douyin-tiktok-download-api-qr
.\Install-QR.ps1
```

安装完成后打开脚本输出的本机初始化链接，设置自己的管理员账号，然后进入 **身份池 → 扫码登录抖音**。默认控制台地址为 `http://127.0.0.1:8000`。

[完整 Docker 安装与维护指南](documents/qr/DOCKER.md) 包括 Linux 命令、端口设置、备份、更新和排查。

## 本版包含

- API、后台任务、数据库、Redis、下载器和扫码浏览器全部由 Docker Compose 管理。
- 扫码确认后获取 Cookie 与匹配的浏览器信息，加密保存并自动导入身份池。
- 重复导入不复制身份；重新扫码替换旧身份并保留轮换设置。
- 来源期限持久保存，列表、到期提醒和备份保持一致。
- 刷新页面后恢复待导入会话；按实际失败操作重试导入、取消、创建和验证码提交。
- 扫码服务使用独立网络、非 root 用户和 Chromium sandbox；不会读取宿主机的个人浏览器资料。
- 初始密钥在本机随机生成，已有配置不会被安装器覆盖。

## 源码位置

| 路径 | 内容 |
|---|---|
| `src/dtk` | 下载 API、身份池及扫码桥接 |
| `web` | React 控制台 |
| `services/qr-login` | 独立扫码服务及测试 |
| `compose.qr.yaml` | 本版完整 Docker 安装方式 |
| `docker/Dockerfile.qr-*` | 应用与扫码服务镜像 |
| `scripts/init_qr.py` | 无第三方依赖的配置生成器 |
| `Install-QR.ps1` | Windows 安装入口 |

本仓库不包含账号、Cookie、密钥、数据库或下载文件。请保管 `.env.qr` 和持久卷，只使用自己的账号及有权处理的内容。

本版保留自定义数据库迁移 `0017/0018`，不要直接替换为上游官方镜像；自动更新提醒默认关闭。版本来源、修改说明和第三方归属见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证与上游

沿用 Apache-2.0，原始版权声明见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。本项目与上游维护者无隶属关系，扫码集成和 Docker 打包问题请提交到本仓库 Issues。

原始文档：[English](README.upstream.md) · [简体中文](README.upstream.zh-CN.md)。
