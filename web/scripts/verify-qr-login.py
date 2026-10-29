"""Headless QR dialog regressions; requires Python Playwright and a running console.

Set DTK_UI_USERNAME / DTK_UI_PASSWORD to a test operator. Set DTK_UI_BROWSER
to a Chromium executable when Playwright's bundled browser is not installed.
All QR operations and platform session checks are intercepted; no phone scan
or real identity mutation takes place.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

from playwright.async_api import async_playwright, expect

PREFIX = "/api/v1/admin/qr-login"
SESSION_ID = "f" * 32
ACCOUNT = {
    "id": "a" * 32,
    "label": "QR regression fixture",
    "cookie_count": 3,
    "expires_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
    "expiry_state": "valid",
}


def envelope(data):
    return {"success": True, "data": data, "error": None, "meta": {}}


async def case(context, base, identity_id, name):
    page = await context.new_page()
    stage = "complete" if name in {"historical_complete", "different_operator"} else None
    counts = {"start": 0, "import": 0, "cancel": 0, "code": 0}
    submitted_codes = []
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def session():
        return {
            "id": SESSION_ID,
            "status": stage,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
            "qr_available": False,
            "image_revision": 0,
            "account": ACCOUNT if stage == "complete" else None,
        }

    async def respond(route, data, status=200):
        await route.fulfill(
            status=status, content_type="application/json", body=json.dumps(envelope(data))
        )

    async def fail(route):
        await route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps(
                {
                    "success": False,
                    "data": None,
                    "error": {
                        "code": "UPSTREAM_UNREACHABLE",
                        "message": "Temporary fixture failure",
                        "details": {"reason": "qr_service_unavailable"},
                    },
                    "meta": {},
                }
            ),
        )

    async def intercept(route):
        nonlocal stage
        path = route.request.url.split(base, 1)[-1]
        if path == PREFIX and route.request.method == "GET":
            return await respond(
                route,
                {
                    "configured": True,
                    "accounts": [ACCOUNT] if stage == "complete" else [],
                    "session": session() if stage else None,
                },
            )
        if path == PREFIX + "/sessions":
            counts["start"] += 1
            if name == "busy_start":
                stage = "waiting"
                return await route.fulfill(status=400, content_type="application/json", body=json.dumps({"success": False, "data": None, "error": {"code": "INVALID_PARAM", "message": "A scan is active", "details": {"reason": "qr_busy"}}, "meta": {}}))
            if name == "retry_start" and counts["start"] == 1:
                return await fail(route)
            stage = "waiting"
            return await respond(route, session(), 201)
        if path.endswith("/import"):
            counts["import"] += 1
            if name == "retry_import" and counts["import"] == 1:
                return await fail(route)
            return await respond(route, {"stored": False, "identity_id": identity_id})
        if route.request.method == "DELETE":
            counts["cancel"] += 1
            if name == "retry_cancel" and counts["cancel"] == 1:
                return await fail(route)
            stage = "cancelled"
            return await respond(route, session())
        if path.endswith("/code"):
            counts["code"] += 1
            submitted_codes.append(route.request.post_data_json["code"])
            if counts["code"] == 1:
                return await fail(route)
            stage = "waiting"
            return await respond(route, {"submitted": True})
        raise AssertionError("Unexpected QR fixture request")

    async def fake_platform_check(route):
        return await respond(route, {"task_id": "12345678-1234-1234-1234-123456789abc"}, 202)

    async def fake_task(route):
        return await respond(
            route,
            {
                "task_id": "12345678-1234-1234-1234-123456789abc",
                "state": "done",
                "data": {
                    "logged_in": True,
                    "account_id": "fixture",
                    "reason": "live",
                    "status": 200,
                    "latency_ms": 1,
                    "detail": None,
                },
            },
        )

    async def open_dialog():
        await page.get_by_role("button", name="扫码登录抖音", exact=True).click()
        return page.get_by_role("dialog", name="扫码登录抖音", exact=True)

    try:
        await page.route("**/api/v1/admin/qr-login**", intercept)
        await page.route("**/api/v1/admin/identities/*/session", fake_platform_check)
        await page.route("**/api/v1/tasks/12345678-1234-1234-1234-123456789abc", fake_task)
        await page.goto(base + "/identities", wait_until="networkidle")
        close = page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True)
        if await close.count():
            await close.click()
        if name == "different_operator":
            await page.evaluate(
                "id => sessionStorage.setItem('dtk.qr-login.pending:another-operator', id)",
                SESSION_ID,
            )
        dialog = await open_dialog()
        if name in {"historical_complete", "different_operator"}:
            await expect(dialog.get_by_role("status")).to_have_text("登录已完成，可以导入身份池。")
            await page.wait_for_timeout(2500)
            assert counts["import"] == 0
        else:
            await dialog.get_by_role("button", name="获取登录二维码", exact=True).click()
            if name in {"retry_start", "busy_start"}:
                await (
                    dialog.get_by_role("alert")
                    .get_by_role("button", name="重试", exact=True)
                    .click()
                )
            await expect(dialog.get_by_role("status")).to_have_text(
                "请用手机抖音扫一扫，并在手机上确认登录。"
            )
            if name == "reload_after_confirmation":
                stage = "complete"
                await page.reload(wait_until="networkidle")
                dialog = await open_dialog()
                await expect(dialog).not_to_be_visible(timeout=12000)
                assert counts["import"] == 1
                # A completed import clears the tab marker and does not repeat on reopening.
                await page.goto(base + "/identities", wait_until="networkidle")
                dialog = await open_dialog()
                await expect(dialog.get_by_role("status")).to_have_text(
                    "登录已完成，可以导入身份池。"
                )
                await page.wait_for_timeout(2500)
                assert counts["import"] == 1
            elif name == "retry_import":
                stage = "complete"
                alert = dialog.get_by_role("alert")
                await expect(alert).to_be_visible(timeout=12000)
                assert counts["import"] == 1
                await alert.get_by_role("button", name="重试", exact=True).click()
                await expect(dialog).not_to_be_visible(timeout=12000)
                assert counts["import"] == 2
            elif name == "retry_cancel":
                await dialog.get_by_role("button", name="取消扫码", exact=True).click()
                await (
                    dialog.get_by_role("alert")
                    .get_by_role("button", name="重试", exact=True)
                    .click()
                )
                await expect(dialog.get_by_role("status")).to_have_text("本次扫码已取消。")
                assert counts["cancel"] == 2
                assert (
                    await page.evaluate(
                        "Object.keys(sessionStorage).filter(k => k.startsWith('dtk.qr-login.pending:')).length"
                    )
                    == 0
                )
            elif name == "retry_code":
                stage = "verification_required"
                await expect(dialog.get_by_role("status")).to_have_text(
                    "平台要求验证，请输入收到的验证码。", timeout=12000
                )
                code_input = dialog.get_by_label("手机验证码")
                await code_input.fill("123456", timeout=12000)
                await dialog.get_by_role("button", name="提交验证码", exact=True).click()
                await (
                    dialog.get_by_role("alert")
                    .get_by_role("button", name="重试", exact=True)
                    .click()
                )
                await expect(code_input).to_have_value("")
                assert submitted_codes == ["123456", "123456"]
            else:
                assert counts["start"] == (1 if name == "busy_start" else 2)
                await expect(dialog.get_by_role("alert")).not_to_be_visible()
        assert not page_errors, page_errors
        return {"case": name, "passed": True, "requests": counts}
    finally:
        await page.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--case", action="append", dest="cases")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=os.environ.get("DTK_UI_BROWSER")
        )
        try:
            context = await browser.new_context(
                locale="zh-CN", viewport={"width": 1280, "height": 960}
            )
            login = await context.request.post(
                base + "/api/v1/auth/login",
                data={
                    "username": os.environ["DTK_UI_USERNAME"],
                    "password": os.environ["DTK_UI_PASSWORD"],
                },
            )
            assert login.status == 200, "Test operator login failed"
            rows = (await (await context.request.get(base + "/api/v1/admin/identities")).json())[
                "data"
            ]
            assert rows, "The test console needs one identity for the post-import detail drawer"
            results = []
            for name in args.cases or [
                "reload_after_confirmation",
                "retry_import",
                "historical_complete",
                "different_operator",
                "retry_start",
                "busy_start",
                "retry_cancel",
                "retry_code",
            ]:
                results.append(await case(context, base, rows[0]["id"], name))
            print(json.dumps({"cases": results, "passed": len(results)}, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
