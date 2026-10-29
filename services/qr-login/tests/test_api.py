import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from cookie_service.app import create_app
from tests.test_vault import cookies


class WaitingDriver:
    async def run(self, session):
        session.publish_qr(b"\x89PNG\r\n\x1a\nTEST_QR")
        await asyncio.Event().wait()


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, WaitingDriver())
    with TestClient(app, base_url="http://127.0.0.1:8790") as client:
        yield client


def login(client):
    assert client.get("/").status_code == 200
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    return {"X-DY-CSRF": response.json()["csrf_token"], "Origin": "http://127.0.0.1:8790"}


def test_api_requires_authorization_and_rejects_foreign_sites(client):
    assert client.get("/api/accounts").status_code == 401
    login(client)
    assert client.get("/api/accounts", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/logins", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/logins", json={}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_create_busy_cancel_and_reload_preserve_active_session(client):
    headers = login(client)
    response = client.post("/api/logins", json={"label": "测试账号"}, headers=headers)
    assert response.status_code == 202
    session_id = response.json()["id"]
    assert client.get("/api/bootstrap").json()["session"]["id"] == session_id
    assert client.post("/api/logins", json={}, headers=headers).status_code == 409
    result = client.delete(f"/api/logins/{session_id}", headers=headers)
    assert result.status_code == 200
    assert result.json()["status"] == "cancelled"
    assert client.get(f"/api/logins/{session_id}/qr").status_code == 404


def test_saved_secrets_are_exported_only_through_explicit_authorized_endpoint(client):
    headers = login(client)
    record = client.app.state.vault.save(cookies(), {"user_agent": "Chrome/155"}, "账号")
    public = client.get("/api/accounts")
    assert "TEST_LOGIN_SECRET" not in public.text
    response = client.get(f"/api/accounts/{record['id']}/export")
    assert response.status_code == 200
    assert "TEST_LOGIN_SECRET" in response.json()["cookie_header"]
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert client.delete(f"/api/accounts/{record['id']}", headers=headers).status_code == 200
    assert client.get(f"/api/accounts/{record['id']}/export").status_code == 404


def test_validation_never_echoes_an_invalid_verification_code(client):
    headers = login(client)
    response = client.post("/api/logins/invalid/code", json={"code": "PRIVATE_OTP_VALUE"}, headers=headers)
    assert response.status_code == 422
    assert "PRIVATE_OTP_VALUE" not in response.text


def test_backend_bearer_token_is_supported_without_browser_csrf(client):
    token = client.app.state.vault.api_token
    response = client.get("/api/accounts", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert client.get("/api/accounts", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_expiry_notice_metadata_and_security_headers(client):
    login(client)
    response = client.get("/api/accounts")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "TEST_LOGIN_SECRET" not in json.dumps(client.get("/api/bootstrap").json())
