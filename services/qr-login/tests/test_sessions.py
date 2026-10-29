import asyncio
import json

import pytest

from cookie_service.sessions import BusySession, LoginManager
from cookie_service.vault import Vault
from tests.test_vault import cookies


class WaitingDriver:
    def __init__(self):
        self.closed = asyncio.Event()

    async def run(self, session):
        try:
            session.publish_qr(b"test-image")
            await asyncio.Event().wait()
        finally:
            self.closed.set()


@pytest.mark.asyncio
async def test_only_one_live_session_and_cancel_closes_browser(tmp_path):
    driver = WaitingDriver()
    manager = LoginManager(Vault(tmp_path), driver)
    session = await manager.start("测试账号")
    await asyncio.sleep(0)
    with pytest.raises(BusySession):
        await manager.start("重复会话")
    await manager.cancel(session.id)
    assert session.status == "cancelled"
    assert driver.closed.is_set()
    assert session.qr_image is None
    assert manager.vault.list_accounts() == []
    await manager.close()


@pytest.mark.asyncio
async def test_immediate_cancel_before_browser_task_starts_is_safe(tmp_path):
    manager = LoginManager(Vault(tmp_path), WaitingDriver())
    session = await manager.start("立即取消")
    await manager.cancel(session.id)
    assert session.status == "cancelled"
    assert manager.vault.list_accounts() == []


@pytest.mark.asyncio
async def test_expired_session_is_not_saved_and_does_not_keep_browser_alive(tmp_path):
    driver = WaitingDriver()
    manager = LoginManager(Vault(tmp_path), driver, ttl=0.02)
    session = await manager.start("过期账号")
    await session.task
    assert session.status == "expired"
    assert driver.closed.is_set()
    assert manager.vault.list_accounts() == []


@pytest.mark.asyncio
async def test_success_is_saved_with_ua_and_status_never_exposes_cookie(tmp_path):
    class SuccessfulDriver:
        async def run(self, session):
            session.publish_qr(b"test-image")
            return cookies(), {"user_agent": "Chrome/155", "browser_version": "155"}

    manager = LoginManager(Vault(tmp_path), SuccessfulDriver())
    session = await manager.start("账号")
    await session.task
    assert session.status == "complete"
    assert session.account["cookie_count"] == 4
    assert "TEST_LOGIN_SECRET" not in json.dumps(session.public())
    assert session.qr_image is None
    assert manager.vault.export(session.account["id"])["user_agent"] == "Chrome/155"


@pytest.mark.asyncio
async def test_upstream_failure_does_not_leak_exception_details(tmp_path):
    class FailedDriver:
        async def run(self, session):
            raise RuntimeError("Cookie: TEST_LOGIN_SECRET")

    manager = LoginManager(Vault(tmp_path), FailedDriver())
    session = await manager.start("失败账号")
    await session.task
    assert session.status == "error"
    assert "TEST_LOGIN_SECRET" not in json.dumps(session.public())
    assert manager.vault.list_accounts() == []
