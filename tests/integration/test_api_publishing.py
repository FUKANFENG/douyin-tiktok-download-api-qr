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
