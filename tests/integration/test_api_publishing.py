import httpx
import pytest

from dtk.core.types import UserRole
from tests.integration import test_api_support as support

api_app = support.api_app
client = support.client
pytestmark = pytest.mark.integration
BASE = "/api/v1/admin/publishing"
JOB = "b" * 32
ACCOUNT = "a" * 32


def job():
    return {
        "id": JOB,
        "account_id": ACCOUNT,
        "title": "Fixture",
        "description": "",
        "filename": "test.mp4",
        "size": 16,
        "state": "ready",
        "account_label": "Shared",
        "account_name": "Fixture Creator",
        "account_uid": "42",
        "created_at": "2026-10-30T00:00:00Z",
        "updated_at": "2026-10-30T00:00:00Z",
        "storage_state": {"secret": "NEVER_EXPOSE_THIS_LOGIN"},
    }


async def test_publishing_requires_admin_and_confirmation(client):
    assert (await client.get(BASE)).status_code == 401
    await support.signed_in(client, role=UserRole.OPERATOR)
    assert (await client.get(BASE)).status_code == 403
    assert (
        await client.post(
            f"{BASE}/jobs/{JOB}/publish", json={"account_id": ACCOUNT, "confirm": True}
        )
    ).status_code == 403


async def test_public_job_filters_credentials_and_rejects_unconfirmed_submit(client, monkeypatch):
    from dtk.api.routes.admin import publishing

    calls = []

    async def remote(_request, method, path, body=None, **kwargs):
        calls.append((method, path))
        return httpx.Response(200, json=job())

    monkeypatch.setattr(publishing, "service_response", remote)
    await support.signed_in(client)
    response = await client.get(f"{BASE}/jobs/{JOB}")
    assert response.status_code == 200
    assert "NEVER_EXPOSE_THIS_LOGIN" not in response.text
    assert "storage_state" not in response.text
    calls.clear()
    denied = await client.post(
        f"{BASE}/jobs/{JOB}/publish", json={"account_id": ACCOUNT, "confirm": False}
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "INVALID_PARAM"
    assert calls == []


async def test_authenticated_binary_video_exceeds_json_ceiling_without_buffering(
    client, monkeypatch
):
    from dtk.api.routes.admin import publishing

    received = 0

    async def remote(_request, method, path, body=None, **kwargs):
        nonlocal received
        assert method == "PUT" and path.endswith("/video")
        assert body is None
        async for chunk in kwargs["content"]:
            received += len(chunk)
        return httpx.Response(200, json={**job(), "state": "uploaded"})

    monkeypatch.setattr(publishing, "service_response", remote)
    await support.signed_in(client)

    async def video():
        for _ in range(32):
            yield b"x" * 65536

    response = await client.put(f"{BASE}/jobs/{JOB}/video", content=video())
    assert response.status_code == 200
    assert received == 2 * 1024 * 1024
    # No other JSON endpoint inherits the video route's larger ceiling.
    refused = await client.post(f"{BASE}/jobs", content=b"x" * (1024 * 1024 + 1))
    assert refused.status_code == 413


async def test_topic_routes_require_admin_and_explicit_draft_upload(client, monkeypatch):
    from dtk.api.routes.admin import publishing

    calls = []

    async def remote(_request, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "123",
                        "name": "Food",
                        "source": "search",
                        "views": 42,
                        "storage_state": "private",
                    }
                ]
            }
            if path == "/api/publishing/topics/search"
            else job(),
        )

    monkeypatch.setattr(publishing, "service_response", remote)
    assert (
        await client.post(BASE + "/topics/search", json={"account_id": ACCOUNT, "keyword": "Food"})
    ).status_code == 401
    await support.signed_in(client, role=UserRole.OPERATOR)
    assert (
        await client.post(BASE + "/topics/search", json={"account_id": ACCOUNT, "keyword": "Food"})
    ).status_code == 403
    assert calls == []
    await support.signed_in(client, username="topic-admin")
    result = await client.post(
        BASE + "/topics/search", json={"account_id": ACCOUNT, "keyword": "Food"}
    )
    assert result.status_code == 200
    assert result.json()["data"]["items"][0]["id"] == "123"
    assert "private" not in result.text
    calls.clear()
    assert (await client.post(f"{BASE}/jobs/{JOB}/recommend", json={})).status_code == 400
    assert calls == []
    assert (
        await client.post(f"{BASE}/jobs/{JOB}/recommend", json={"confirm_upload": True})
    ).status_code == 200
    assert len(calls) == 1


async def test_topic_validation_and_confirmation_revision_are_forwarded(client, monkeypatch):
    from dtk.api.routes.admin import publishing

    calls = []

    async def remote(_request, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return httpx.Response(200, json=job())

    monkeypatch.setattr(publishing, "service_response", remote)
    await support.signed_in(client)
    rejected = await client.put(
        f"{BASE}/jobs/{JOB}/topics",
        json={"revision": "c" * 32, "topics": [{"name": "Plain text", "source": "search"}]},
    )
    assert rejected.status_code == 400
    assert calls == []
    selected = [{"id": "123", "name": "Food", "source": "search"}]
    assert (
        await client.put(
            f"{BASE}/jobs/{JOB}/topics", json={"revision": "c" * 32, "topics": selected}
        )
    ).status_code == 200
    assert calls[-1][2]["topics"] == selected
    assert (
        await client.post(
            f"{BASE}/jobs/{JOB}/publish",
            json={"account_id": ACCOUNT, "confirm": True, "topics_revision": "c" * 32},
        )
    ).status_code == 200
    assert calls[-1][2]["topics_revision"] == "c" * 32
