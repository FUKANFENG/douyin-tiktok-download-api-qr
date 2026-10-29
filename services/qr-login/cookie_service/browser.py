"""Only an isolated headless browser is used; never the user's browser profile.

API references: https://playwright.dev/python/docs/api/class-browsercontext
Login flow reference: MinG-98/prometheus-relay (MIT), reviewed 2026-10-28.
"""

import asyncio
import base64
import contextlib
import os
import re
from pathlib import Path

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from .sessions import TERMINAL, LoginError, LoginSession
from .vault import usable_login

QR_SELECTORS = ("#animate_qrcode_container img", 'img[aria-label*="二维码"]', '[class*="qrcode"] img')
LOGIN_URL = "https://www.douyin.com/?show_login=1"


def find_browser() -> str | None:
    configured = os.environ.get("DY_COOKIE_BROWSER_PATH")
    if configured:
        return configured if Path(configured).is_file() else None
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    bundled = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"))
    for pattern in ("chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome"):
        candidates.extend(str(path) for path in sorted(bundled.glob(pattern), reverse=True))
    return next((path for path in candidates if Path(path).is_file()), None)


class BrowserLogin:
    def __init__(self, executable: str | None = None):
        self.executable = executable or find_browser()

    async def run(self, session: LoginSession) -> tuple[list[dict], dict]:
        if not self.executable:
            raise LoginError("未找到 Chrome 或 Edge，请安装浏览器或设置 DY_COOKIE_BROWSER_PATH。")
        browser = None
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    executable_path=self.executable,
                    headless=True,
                    chromium_sandbox=os.environ.get("DY_COOKIE_CHROMIUM_SANDBOX", "false").lower() == "true",
                )
                bootstrap = await browser.new_context()
                sample = await bootstrap.new_page()
                user_agent = (await sample.evaluate("navigator.userAgent")).replace(
                    "HeadlessChrome/", "Chrome/"
                )
                await bootstrap.close()
                context = await browser.new_context(
                    user_agent=user_agent,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()
                page.set_default_timeout(2500)
                page.on("response", lambda response: self._capture_state(response, session))
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                opened = False
                while True:
                    cookies = await context.cookies("https://www.douyin.com/")
                    if usable_login(cookies):
                        session.message = "手机已确认，正在收集完整登录信息…"
                        await page.wait_for_timeout(1800)
                        cookies = await context.cookies("https://www.douyin.com/")
                        if not usable_login(cookies):
                            continue
                        cookies = [
                            cookie for cookie in cookies if self._douyin_domain(cookie.get("domain", ""))
                        ]
                        fingerprint = await page.evaluate("""() => ({
                            user_agent: navigator.userAgent, language: navigator.language,
                            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                            platform: navigator.platform,
                            screen: {width: screen.width, height: screen.height},
                            hardware_concurrency: navigator.hardwareConcurrency,
                            device_memory: navigator.deviceMemory ?? null
                        })""")
                        fingerprint.update(browser_family="chrome", browser_version=browser.version)
                        return cookies, fingerprint

                    found_qr = await self._capture_qr(page, session)
                    if not found_qr and not opened:
                        opened = await self._open_login(page)
                    if session.status == "scanned":
                        await self._verification(page, session)
                    elif session.status == "verification_required":
                        await self._verification(page, session)
                    await asyncio.sleep(1)
            except BrowserError as error:
                raise LoginError("登录页面暂时无法访问，请检查网络后重新获取二维码。") from error
            finally:
                if browser:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(browser.close(), timeout=8)

    @staticmethod
    def _douyin_domain(domain: str) -> bool:
        domain = domain.lstrip(".").lower()
        return domain == "douyin.com" or domain.endswith(".douyin.com")

    @staticmethod
    async def _open_login(page) -> bool:
        for locator in [
            page.get_by_role("button", name=re.compile(r"^登录$")),
            page.get_by_text("登录", exact=True),
        ]:
            try:
                await locator.first.click(timeout=1200)
                return True
            except BrowserError:
                continue
        return False

    @staticmethod
    async def _capture_qr(page, session: LoginSession) -> bool:
        for selector in QR_SELECTORS:
            for element in await page.locator(selector).all():
                try:
                    if not await element.is_visible():
                        continue
                    source = await element.get_attribute("src") or ""
                    if source.startswith("data:image/png;base64,"):
                        image = base64.b64decode(source.split(",", 1)[1], validate=True)
                    else:
                        image = await element.screenshot(type="png")
                    if 100 <= len(image) <= 512 * 1024:
                        session.publish_qr(image)
                        return True
                except (BrowserError, ValueError):
                    continue
        return False

    @staticmethod
    async def _capture_state(response, session: LoginSession) -> None:
        if "/check_qrconnect/" not in response.url or session.status in TERMINAL:
            return
        try:
            payload = await response.json()
            status = str(payload.get("data", {}).get("status", ""))
            if status in {"2", "3"} and session.status in {"starting", "waiting"}:
                session.status = "scanned"
                session.message = "已扫码，请在手机上确认登录；如提示验证，请按提示完成。"
        except (BrowserError, ValueError, AttributeError):
            return

    @staticmethod
    async def _verification(page, session: LoginSession) -> None:
        field = page.locator('input[placeholder*="验证码"]').first
        if not await field.is_visible():
            return
        session.status = "verification_required"
        session.message = "抖音要求短信验证，请填写收到的验证码。"
        if session.verification.empty():
            return
        code = session.verification.get_nowait()
        try:
            await field.fill(code)
            await page.get_by_role("button", name=re.compile(r"^(确认|确定|验证|提交|下一步)$")).first.click()
            session.status = "scanned"
            session.message = "验证码已提交，正在等待抖音确认…"
        except BrowserError:
            session.message = "暂时未能提交验证码，请稍后再试。"
