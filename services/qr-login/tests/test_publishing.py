import json

import pytest

from cookie_service.publishing import PublishManager, PublishProblem
from cookie_service.vault import Vault
from tests.test_vault import cookies

VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(64)


class Driver:
    def __init__(self):
        self.sent = 0

    async def check(self, record):
        return {
            "storage_state": {"cookies": record["cookies"], "origins": []},
            "uid": "user-one",
            "nickname": "Account One",
        }

    async def publish(self, record, job, path, on_submitting):
        on_submitting()
        self.sent += 1
        return {
            "storage_state": {"cookies": record["cookies"], "origins": []},
            "post_id": "1234567890123456789",
        }


def setup(tmp_path, driver=None):
    vault = Vault(tmp_path)
    account = vault.save(cookies(), {"user_agent": "Chrome/155"}, "shared")
    return PublishManager(vault, driver or Driver()), account


async def chunks(data=VIDEO):
    yield data[:10]
    yield data[10:]


async def ready(manager, account, key="a" * 32):
    job = manager.create(
        {
            "account_id": account["id"],
            "title": "Test video",
            "description": "",
            "filename": "video.mp4",
            "size": len(VIDEO),
            "client_key": key,
        }
    )
    await manager.receive(job["id"], chunks())
    manager.prepare(job["id"])
    await manager.wait()
    assert manager.get(job["id"])["state"] == "ready"
    return manager.get(job["id"])


@pytest.mark.asyncio
async def test_prepare_does_not_publish_and_double_submit_sends_once(tmp_path):
    driver = Driver()
    manager, account = setup(tmp_path, driver)
    job = await ready(manager, account)
    assert driver.sent == 0
    assert job["account_name"] == "Account One"
    manager.submit(job["id"], account["id"])
    manager.submit(job["id"], account["id"])
    await manager.wait()
    result = manager.get(job["id"])
    assert result["state"] == "published"
    assert result["post_id"] == "1234567890123456789"
    assert driver.sent == 1
    assert "TEST_LOGIN_SECRET" not in json.dumps(result)
    assert PublishManager(manager.vault, Driver()).get(job["id"])["state"] == "published"


@pytest.mark.asyncio
async def test_new_scan_or_wrong_account_cannot_be_published_from_old_confirmation(tmp_path):
    manager, account = setup(tmp_path)
    job = await ready(manager, account)
    with pytest.raises(PublishProblem):
        manager.submit(job["id"], "f" * 32)
    manager.vault.save(cookies(), {"user_agent": "Chrome/156"}, "renewed", account["id"])
    with pytest.raises(PublishProblem):
        manager.submit(job["id"], account["id"])
    assert manager.driver.sent == 0


@pytest.mark.asyncio
async def test_uncertain_submission_is_never_retried(tmp_path):
    class Interrupted(Driver):
        async def publish(self, record, job, path, on_submitting):
            on_submitting()
            self.sent += 1
            raise TimeoutError()

    driver = Interrupted()
    manager, account = setup(tmp_path, driver)
    job = await ready(manager, account)
    manager.submit(job["id"], account["id"])
    await manager.wait()
    assert manager.get(job["id"])["state"] == "uncertain"
    manager.submit(job["id"], account["id"])
    assert driver.sent == 1
    restarted = PublishManager(manager.vault, Driver())
    assert restarted.get(job["id"])["state"] == "uncertain"
    assert restarted.driver.sent == 0


@pytest.mark.asyncio
async def test_upload_size_and_file_signature_are_checked(tmp_path):
    manager, account = setup(tmp_path)
    job = manager.create(
        {
            "account_id": account["id"],
            "title": "Test",
            "description": "",
            "filename": "clip.mp4",
            "size": len(VIDEO),
            "client_key": "b" * 32,
        }
    )
    with pytest.raises(PublishProblem):
        await manager.receive(job["id"], chunks(VIDEO + b"extra"))
    assert manager.get(job["id"])["state"] == "failed"
    assert not manager.video_path(job["id"]).exists()


@pytest.mark.asyncio
async def test_cancelled_upload_is_deleted_and_never_auto_submitted(tmp_path):
    manager, account = setup(tmp_path)
    job = await ready(manager, account)
    manager.cancel(job["id"])
    assert manager.get(job["id"])["state"] == "cancelled"
    assert not manager.video_path(job["id"]).exists()
    manager.submit(job["id"], account["id"])
    assert manager.driver.sent == 0


@pytest.mark.asyncio
async def test_restart_during_final_submission_preserves_uncertainty(tmp_path):
    manager, account = setup(tmp_path)
    job = await ready(manager, account)
    manager.update(job["id"], state="submitting")
    restarted = PublishManager(manager.vault, Driver())
    assert restarted.get(job["id"])["state"] == "uncertain"
    restarted.submit(job["id"], account["id"])
    assert restarted.driver.sent == 0
