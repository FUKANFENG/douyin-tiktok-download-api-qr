# Upstream and local changes

This is an unofficial derivative of [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API), based on **v5.1.7**, commit `203e08fc33278aa3d17bfb09a06e4fcfd3e2c43c`. The upstream Apache-2.0 license, copyright notice, source, and documentation are retained.

The QR edition adds a separately isolated Douyin login service, a protected console bridge, a QR login dialog, durable login expiry, backup/restore of local account links, reload recovery, and operation-specific retries. Database revisions `0017` and `0018` belong to this edition.

The Docker distribution adds `compose.qr.yaml`, two Docker build recipes, a configuration generator, a Windows installer, and deployment tests. It builds the frontend inside Docker and runs the browser without a desktop or an existing personal browser profile. The standalone QR service source lives in `services/qr-login`.

The local QR service is independently implemented. Its login flow was informed by the public MIT-licensed [prometheus-relay login module](https://github.com/MinG-98/prometheus-relay/blob/main/webapp/qr_login.py); no message-sending or scheduling code was copied from it.

`docker/qr-seccomp.json` is based on [Microsoft Playwright v1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json), distributed under Apache-2.0. It enables the user namespaces required by Chromium's sandbox while retaining the default deny policy. This edition also allows the `chroot` syscall inside Chromium's user namespace so the browser works with all container capabilities dropped. Playwright itself and Chromium retain their respective licenses.

The QR edition is pinned to the upstream 5.1.7 dependency set. Do not replace this image with a stock upstream release: the stock migration graph does not contain this edition's `0017` and `0018` revisions. Update by reviewing and merging upstream changes, retaining the custom migrations and tests, and rebuilding this edition.
