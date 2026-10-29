from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cookie_service.app import create_app
from cookie_service.browser import find_browser
from cookie_service.vault import Vault
from tests.test_api import WaitingDriver
from tests.test_vault import cookies


def test_container_api_requires_bearer_and_does_not_issue_web_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DY_COOKIE_WEB_UI", "false")
    monkeypatch.setenv("DY_COOKIE_API_TOKEN", "container-test-token-" + "a" * 44)
    app = create_app(tmp_path, WaitingDriver())
    with TestClient(app, base_url="http://127.0.0.1:8790") as client:
        root = client.get("/")
        assert root.status_code == 404
        assert "set-cookie" not in root.headers
        assert client.get("/api/accounts").status_code == 401
        token = app.state.vault.api_token
        client.cookies.set("dy_cookie_service", token)
        assert client.get("/api/accounts").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        started = client.post("/api/logins", headers=headers, json={"label": "fixture"})
        assert started.status_code == 202
        cancelled = client.delete(f"/api/logins/{started.json()['id']}", headers=headers)
        assert cancelled.json()["status"] == "cancelled"


def test_api_token_rotation_preserves_encrypted_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("DY_COOKIE_API_TOKEN", "a" * 64)
    original = Vault(tmp_path)
    account = original.save(cookies(), {"user_agent": "Chrome/155"}, "fixture")
    key = (tmp_path / "vault.key").read_bytes()
    monkeypatch.setenv("DY_COOKIE_API_TOKEN", "b" * 64)
    reopened = Vault(tmp_path)
    assert reopened.api_token != original.api_token
    assert reopened.export(account["id"])["label"] == "fixture"
    assert (tmp_path / "vault.key").read_bytes() == key


def test_short_api_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DY_COOKIE_API_TOKEN", "short")
    with pytest.raises(RuntimeError, match="32-256"):
        Vault(tmp_path)


def test_bundled_chromium_is_discovered(tmp_path, monkeypatch):
    target = tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"
    target.parent.mkdir(parents=True)
    target.touch()
    monkeypatch.delenv("DY_COOKIE_BROWSER_PATH", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(Path, "is_file", lambda self: self == target)
    assert find_browser() == str(target)
