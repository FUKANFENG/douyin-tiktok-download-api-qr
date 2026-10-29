"""The local login bridge imports credentials without returning them to the console."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from dtk.core.db import session_scope
from dtk.core.types import UserRole
from dtk.db.models import Identity
from tests.integration import test_api_support as support

api_app = support.api_app
client = support.client
pytestmark = pytest.mark.integration
BASE = "/api/v1/admin/qr-login"
ACCOUNT = "a" * 32
SECRET = "only-the-backend-may-see-this-session"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def record():
    return {
        "id": ACCOUNT,
        "version": 1,
        "platform": "douyin",
        "label": "Test login",
        "updated_at": "2026-10-28T00:00:00+00:00",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "cookies": [
            {"name": "ttwid", "value": "visitor-cookie-value"},
            {"name": "sessionid", "value": SECRET},
            {"name": "sid_tt", "value": SECRET},
        ],
        "user_agent": UA,
        "fingerprint": {
            "user_agent": UA,
            "platform": "Win32",
            "language": "zh-CN",
            "timezone": "Asia/Shanghai",
            "screen": {"width": 1280, "height": 900},
            "hardware_concurrency": 16,
            "device_memory": 8,
        },
    }


@pytest.fixture
def remote(monkeypatch):
    from dtk.api.routes.admin import qr_login

    saved = record()

    async def request(_request, method, path, body=None):
        assert method == "GET"
        assert path == f"/api/accounts/{ACCOUNT}/export"
        return deepcopy(saved)

    monkeypatch.setattr(qr_login, "service_json", request)
    return saved


async def test_bridge_requires_login(client):
    response = await client.get(BASE)
    assert response.status_code == 401


async def test_viewer_cannot_use_bridge(client):
    await support.signed_in(client, role=UserRole.VIEWER)
    assert (await client.get(BASE)).status_code == 403
    assert (await client.post(f"{BASE}/accounts/{ACCOUNT}/import")).status_code == 403


async def test_unconfigured_bridge_has_no_accounts(client, api_app):
    await support.signed_in(client)
    api_app.state.settings.qr_login_url = ""
    api_app.state.settings.qr_login_token = ""
    response = await client.get(BASE)
    assert response.status_code == 200
    assert response.json()["data"] == {"configured": False, "accounts": [], "session": None}


async def test_import_is_encrypted_preserves_fingerprint_and_is_idempotent(client, api_app, remote):
    await support.signed_in(client)
    first = await client.post(f"{BASE}/accounts/{ACCOUNT}/import")
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["stored"] is True
    assert SECRET not in first.text
    second = await client.post(f"{BASE}/accounts/{ACCOUNT}/import")
    assert second.json()["data"]["identity_id"] == data["identity_id"]
    assert second.json()["data"]["stored"] is False
    async with session_scope() as db:
        row = await db.get(Identity, UUID(data["identity_id"]))
        assert SECRET.encode() not in row.cookies_encrypted
        assert SECRET in api_app.state.cipher.decrypt(row.cookies_encrypted, aad=str(row.id))
        assert row.fingerprint["screen"] == "1280x900"
        assert row.fingerprint["hardware_concurrency"] == 16
        assert row.fingerprint["timezone"] == "Asia/Shanghai"
        assert row.fingerprint["user_agent"] == UA
        assert row.proxy_id is None
        assert row.authenticated is True


async def test_updated_login_replaces_old_identity_and_preserves_rotation(client, remote):
    await support.signed_in(client)
    first = (await client.post(f"{BASE}/accounts/{ACCOUNT}/import")).json()["data"]
    async with session_scope() as db:
        old = await db.get(Identity, UUID(first["identity_id"]))
        old.rotation_endpoints = []
    remote["updated_at"] = "2026-10-28T01:00:00+00:00"
    remote["cookies"][1]["value"] = SECRET + "-renewed"
    second = (await client.post(f"{BASE}/accounts/{ACCOUNT}/import")).json()["data"]
    assert second["identity_id"] != first["identity_id"]
    async with session_scope() as db:
        old = await db.get(Identity, UUID(first["identity_id"]))
        new = await db.get(Identity, UUID(second["identity_id"]))
        assert old.state == "retired"
        assert old.cookies_encrypted == b""
        assert new.rotation_endpoints == []


@pytest.mark.parametrize("invalid", ["expired", "guest", "wrong-account", "wrong-platform"])
async def test_invalid_export_is_rejected_without_leaking_credentials(client, remote, invalid):
    await support.signed_in(client)
    if invalid == "expired":
        remote["expires_at"] = "2000-01-01T00:00:00+00:00"
    elif invalid == "guest":
        remote["cookies"] = remote["cookies"][:1]
    elif invalid == "wrong-account":
        remote["id"] = "b" * 32
    else:
        remote["platform"] = "tiktok"
    response = await client.post(f"{BASE}/accounts/{ACCOUNT}/import")
    assert response.status_code == 400, response.text
    assert SECRET not in response.text


async def test_concurrent_imports_create_one_identity(client, remote):
    await support.signed_in(client)
    responses = await asyncio.gather(
        *(client.post(f"{BASE}/accounts/{ACCOUNT}/import") for _ in range(4))
    )
    assert all(response.status_code == 200 for response in responses)
    data = [response.json()["data"] for response in responses]
    assert len({row["identity_id"] for row in data}) == 1
    assert sum(row["stored"] for row in data) == 1


async def test_bootstrap_filters_credentials_and_disables_caching(client, api_app, monkeypatch):
    from dtk.api.routes.admin import qr_login

    await support.signed_in(client)
    api_app.state.settings.qr_login_url = "http://localhost:8790"
    api_app.state.settings.qr_login_token = "private-bridge-token"

    async def bootstrap(*args):
        return {
            "csrf_token": "private-csrf",
            "accounts": [
                {
                    **record(),
                    "cookie_count": 3,
                    "expiry_state": "valid",
                    "cookie_header": SECRET,
                }
            ],
            "session": None,
        }

    monkeypatch.setattr(qr_login, "service_json", bootstrap)
    response = await client.get(BASE)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["accounts"][0]["cookie_count"] == 3
    for secret in [SECRET, "private-csrf", "private-bridge-token", UA]:
        assert secret not in response.text


async def test_retired_identity_can_be_reimported(client, remote):
    await support.signed_in(client)
    first = (await client.post(f"{BASE}/accounts/{ACCOUNT}/import")).json()["data"]
    async with session_scope() as db:
        row = await db.get(Identity, UUID(first["identity_id"]))
        row.state = "retired"
        row.cookies_encrypted = b""
    response = await client.post(f"{BASE}/accounts/{ACCOUNT}/import")
    assert response.status_code == 200
    assert response.json()["data"]["identity_id"] != first["identity_id"]


async def test_browser_cookie_with_empty_name_does_not_reject_valid_login(client, remote):
    await support.signed_in(client)
    remote["cookies"].append({"name": "", "value": "nameless-browser-artifact"})
    response = await client.post(f"{BASE}/accounts/{ACCOUNT}/import")
    assert response.status_code == 200, response.text
    assert "nameless-browser-artifact" not in response.text


async def test_empty_session_marker_is_not_an_authenticated_login(client, remote):
    await support.signed_in(client)
    remote["cookies"][1]["value"] = ""
    response = await client.post(f"{BASE}/accounts/{ACCOUNT}/import")
    assert response.status_code == 400
