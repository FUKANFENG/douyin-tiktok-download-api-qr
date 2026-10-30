import time

from fastapi.testclient import TestClient

from cookie_service.app import create_app
from tests.test_publishing import VIDEO, Driver
from tests.test_vault import cookies


def test_authenticated_video_flow_requires_explicit_final_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("DY_COOKIE_WEB_UI", "false")
    driver = Driver()
    app = create_app(tmp_path, publisher=driver)
    account = app.state.vault.save(cookies(), {"user_agent": "Chrome/155"}, "shared")
    with TestClient(app, base_url="http://127.0.0.1:8790") as client:
        base = "/api/publishing"
        assert client.get(base).status_code == 401
        headers = {"Authorization": "Bearer " + app.state.vault.api_token}
        body = {
            "account_id": account["id"],
            "client_key": "c" * 32,
            "title": "Fixture video",
            "filename": "clip.mp4",
            "size": len(VIDEO),
        }
        created = client.post(base + "/jobs", json=body, headers=headers)
        assert created.status_code == 201
        job = created.json()
        assert client.post(base + "/jobs", json=body, headers=headers).json()["id"] == job["id"]
        path = base + "/jobs/" + job["id"]
        assert client.put(path + "/video", content=VIDEO, headers=headers).status_code == 200
        assert client.post(path + "/prepare", headers=headers).status_code == 200
        for _ in range(100):
            status = client.get(path, headers=headers).json()
            if status["state"] == "ready":
                break
            time.sleep(0.01)
        assert status["state"] == "ready"
        assert driver.sent == 0
        assert (
            client.post(path + "/publish", json={"account_id": account["id"]}, headers=headers).status_code
            == 422
        )
        assert (
            client.post(
                path + "/publish", json={"account_id": account["id"], "confirm": True}, headers=headers
            ).status_code
            == 200
        )
        for _ in range(100):
            status = client.get(path, headers=headers).json()
            if status["state"] == "published":
                break
            time.sleep(0.01)
        assert status["state"] == "published"
        assert driver.sent == 1
        assert "TEST_LOGIN_SECRET" not in client.get(base, headers=headers).text
