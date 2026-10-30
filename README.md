# DTK · 抖音采集与视频发布

基于 [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) **v5.1.7** 的非官方定制版，整合 [social-auto-upload](https://github.com/dreammis/social-auto-upload) 的抖音视频发布流程。保留抖音、TikTok、Bilibili API 和下载控制台，提供统一的抖音扫码登录、采集身份池与视频发布页面。

**登录一次，采集和发布共用同一份加密登录状态。** 发布视频时先检查真实创作者账号，再明确确认提交；不使用采集账号轮换。

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
- 管理员可在“发布视频”选择账号、上传 MP4/MOV/M4V、填写标题和正文，并查看持久化发布记录。
- 完整浏览器会话加密保存，检查与发布后同步采集登录态；兼容早期只含 Cookie 的扫码记录。
- 每个任务只提交一次；断线或重启后结果不确定时保留状态，不自动重发。
- 支持搜索并选择原生话题、明确添加自定义话题，以及确认草稿上传后获取视频推荐话题；发布前校验实际话题关联。

[视频发布与 Agent API 使用说明](documents/qr/PUBLISHING.md) 包括确认流程、数据保存和异常处理。当前发布功能仅支持抖音视频，不包含小红书、图文、定时或批量发布。

## 源码位置

| 路径 | 内容 |
|---|---|
| `src/dtk` | 下载 API、身份池及扫码桥接 |
| `web` | React 控制台 |
| `services/qr-login` | 共用加密账号、扫码服务、抖音发布与测试 |
| `compose.qr.yaml` | 本版完整 Docker 安装方式 |
| `docker/Dockerfile.qr-*` | 应用与扫码服务镜像 |
| `scripts/init_qr.py` | 无第三方依赖的配置生成器 |
| `Install-QR.ps1` | Windows 安装入口 |

本仓库不包含账号、Cookie、密钥、数据库或下载文件。请保管 `.env.qr` 和持久卷，只使用自己的账号及有权处理的内容。

本版保留自定义数据库迁移 `0017/0018`，不要直接替换为上游官方镜像；自动更新提醒默认关闭。版本来源、修改说明和第三方归属见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证与上游

沿用 Apache-2.0，原始版权声明见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。本项目与上游维护者无隶属关系，扫码集成和 Docker 打包问题请提交到本仓库 Issues。

抖音上传页面操作改编自 social-auto-upload，保留其 [MIT 许可证](third_party/social-auto-upload-LICENSE)。

原始文档：[English](README.upstream.md) · [简体中文](README.upstream.zh-CN.md)。
