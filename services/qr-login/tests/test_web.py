"""Exercise the real webpage with controlled login results, without a real account."""

import asyncio
import json
import threading
import time

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from cookie_service.app import create_app
from cookie_service.browser import find_browser
from tests.test_vault import cookies


@pytest.fixture
def live_app(tmp_path):
    class CompletedLogin:
        async def run(self, session):
            await asyncio.sleep(0.15)
            return cookies(), {"user_agent": "Mozilla/5.0 Chrome/155.0.0.0", "browser_version": "155"}

    import socket

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    app = create_app(tmp_path, CompletedLogin())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.03)
        assert server.started
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=8)


@pytest.mark.asyncio
async def test_webpage_saves_exports_and_refreshes_one_account_without_leaking_secrets(live_app, tmp_path):
    executable = find_browser()
    if not executable:
        pytest.skip("This UI test requires installed Chrome or Edge")
    url, app = live_app
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 1000}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        await page.goto(url, wait_until="networkidle")
        await page.get_by_label("账号备注").fill("网页测试账号")
        await page.get_by_role("button", name="获取登录二维码", exact=True).click()
        await expect(page.locator(".account-row")).to_have_count(1)
        await expect(page.locator(".account-name")).to_have_text("网页测试账号")
        assert "TEST_LOGIN_SECRET" not in await page.locator("body").inner_text()
        original_id = app.state.vault.list_accounts()[0]["id"]
        async with page.expect_download() as event:
            await page.get_by_role("button", name="导出登录信息", exact=True).click()
        download = await event.value
        export_path = tmp_path / "export.json"
        await download.save_as(export_path)
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert exported["user_agent"] == "Mozilla/5.0 Chrome/155.0.0.0"
        assert "TEST_LOGIN_SECRET" in exported["cookie_header"]
        await page.get_by_role("button", name="重新扫码", exact=True).click()
        await expect(page.locator("#status-title")).to_have_text("已保存")
        await expect(page.locator(".account-row")).to_have_count(1)
        assert app.state.vault.list_accounts()[0]["id"] == original_id
        await page.set_viewport_size({"width": 320, "height": 960})
        dimensions = await page.evaluate(
            "() => ({width: innerWidth, content: document.documentElement.scrollWidth})"
        )
        assert dimensions["content"] <= dimensions["width"]
        assert not errors
        await browser.close()
