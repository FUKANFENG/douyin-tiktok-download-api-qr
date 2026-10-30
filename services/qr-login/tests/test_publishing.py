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


@pytest.mark.asyncio
async def test_changing_topics_invalidates_an_old_confirmation_and_survives_restart(tmp_path):
    manager, account = setup(tmp_path)
    job = await ready(manager, account)
    old_revision = job["topics_revision"]
    chosen = [{"id": "123", "name": "美食", "source": "search"}]
    updated = manager.set_topics(job["id"], chosen, old_revision)
    assert updated["topics"] == chosen
    assert updated["topics_revision"] != old_revision
    assert PublishManager(manager.vault, Driver()).get(job["id"])["topics"] == chosen
    with pytest.raises(PublishProblem, match="topics_changed"):
        manager.submit(job["id"], account["id"], old_revision)
    with pytest.raises(PublishProblem, match="topics_changed"):
        manager.set_topics(job["id"], [], old_revision)
    manager.submit(job["id"], account["id"], updated["topics_revision"])
    with pytest.raises(PublishProblem):
        manager.set_topics(job["id"], [], updated["topics_revision"])
    await manager.wait()
    assert manager.driver.sent == 1


@pytest.mark.asyncio
async def test_native_recommendations_are_persisted_without_publishing(tmp_path):
    class WithRecommendations(Driver):
        async def recommend(self, record, job, path):
            assert path.is_file()
            return {
                **await self.check(record),
                "recommendations": [{"id": "123", "name": "美食", "source": "recommend", "views": 42}],
            }

    driver = WithRecommendations()
    manager, account = setup(tmp_path, driver)
    job = await ready(manager, account)
    assert manager.recommend(job["id"])["state"] == "recommending"
    await manager.wait()
    result = manager.get(job["id"])
    assert result["state"] == "ready"
    assert result["recommended_topics"][0]["id"] == "123"
    assert result["topics"] == []  # Recommendations are not silently selected.
    assert result["recommendations_loaded"] is True
    assert driver.sent == 0


@pytest.mark.asyncio
async def test_old_job_records_still_load_with_empty_topic_defaults(tmp_path):
    manager, account = setup(tmp_path)
    job = await ready(manager, account)
    for key in ["topics", "topics_revision", "recommended_topics", "recommendations_loaded"]:
        job.pop(key, None)
    with manager.connect() as db:
        db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
    restored = manager.get(job["id"])
    assert restored["topics"] == []
    assert restored["recommended_topics"] == []
    manager.submit(job["id"], account["id"])
    await manager.wait()
    assert manager.driver.sent == 1


@pytest.mark.asyncio
async def test_blocked_topic_request_remains_retryable_without_ambiguous_submission(tmp_path):
    class Blocked(Driver):
        async def publish(self, record, job, path, on_submitting):
            on_submitting()
            raise PublishProblem("topics_unverified", submission_blocked=True)

    driver = Blocked()
    manager, account = setup(tmp_path, driver)
    job = await ready(manager, account)
    manager.submit(job["id"], account["id"])
    await manager.wait()
    result = manager.get(job["id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "topics_unverified"
    assert manager.video_path(job["id"]).is_file()
    assert driver.sent == 0
    manager.prepare(job["id"])
    await manager.wait()
    assert manager.get(job["id"])["state"] == "ready"


@pytest.mark.asyncio
async def test_renewed_account_clears_old_video_recommendations(tmp_path):
    manager, account = setup(tmp_path)
    job = await ready(manager, account)
    manager.update(
        job["id"],
        recommended_topics=[{"id": "123", "name": "Food", "source": "recommend"}],
        recommendations_loaded=True,
    )
    manager.vault.save(cookies(), {"user_agent": "Chrome/156"}, "renewed", account["id"])
    manager.prepare(job["id"])
    await manager.wait()
    restored = manager.get(job["id"])
    assert restored["recommended_topics"] == []
    assert restored["recommendations_loaded"] is False
