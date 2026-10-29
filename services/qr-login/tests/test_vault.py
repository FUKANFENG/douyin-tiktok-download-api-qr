import json
from datetime import UTC, datetime, timedelta

import pytest

from cookie_service.vault import Vault, login_expiry, usable_login


def cookies():
    issued = int(datetime.now(UTC).timestamp())
    return [
        {"name": "sessionid", "value": "TEST_LOGIN_SECRET", "domain": ".douyin.com", "path": "/"},
        {"name": "sid_tt", "value": "TEST_SID_SECRET", "domain": ".douyin.com", "path": "/"},
        {"name": "ttwid", "value": "TEST_VISITOR_SECRET", "domain": ".douyin.com", "path": "/"},
        {
            "name": "sid_guard",
            "value": f"TEST_GUARD%7C{issued}%7C5184000%7Cdate",
            "domain": ".douyin.com",
            "path": "/",
        },
    ]


def test_guest_or_missing_visitor_is_not_a_complete_login():
    assert not usable_login(cookies()[2:])
    assert not usable_login(cookies()[:2])
    assert usable_login(cookies())
    assert not usable_login([{**item, "value": ""} for item in cookies()])


def test_expiry_uses_guard_and_does_not_mistake_short_guest_tokens_for_login_expiry():
    jar = cookies()
    jar.append({"name": "msToken", "value": "short", "expires": datetime.now(UTC).timestamp() + 60})
    expiry = login_expiry(jar)
    assert expiry is not None
    assert datetime.now(UTC) + timedelta(days=59) < expiry < datetime.now(UTC) + timedelta(days=61)
    assert login_expiry([{"name": "sid_guard", "value": "invalid"}]) is None


def test_encrypted_persistence_and_public_metadata_never_include_cookie_values(tmp_path):
    vault = Vault(tmp_path)
    record = vault.save(
        cookies(),
        {"user_agent": "Mozilla/5.0 Chrome/155.0.0.0", "language": "zh-CN", "timezone": "Asia/Shanghai"},
        "我的抖音",
    )
    public = vault.list_accounts()
    assert len(public) == 1
    assert public[0]["label"] == "我的抖音"
    assert "TEST_LOGIN_SECRET" not in json.dumps(public)
    assert "cookie_header" not in public[0]
    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert b"TEST_LOGIN_SECRET" not in file.read_bytes()
    restored = Vault(tmp_path).export(record["id"])
    assert "sessionid=TEST_LOGIN_SECRET" in restored["cookie_header"]
    assert restored["user_agent"] == "Mozilla/5.0 Chrome/155.0.0.0"


def test_refresh_replaces_one_account_and_preserves_its_label(tmp_path):
    vault = Vault(tmp_path)
    record = vault.save(cookies(), {"user_agent": "Chrome/155"}, "账号一")
    refreshed = [
        {**cookie, "value": "NEW_SECRET"} if cookie["name"] == "sessionid" else cookie for cookie in cookies()
    ]
    updated = vault.save(refreshed, {"user_agent": "Chrome/156"}, "", replace_id=record["id"])
    assert updated["id"] == record["id"]
    assert updated["label"] == "账号一"
    assert len(vault.list_accounts()) == 1
    assert "NEW_SECRET" in vault.export(record["id"])["cookie_header"]


def test_missing_key_does_not_silently_destroy_saved_accounts(tmp_path):
    vault = Vault(tmp_path)
    vault.save(cookies(), {"user_agent": "Chrome/155"}, "账号")
    (tmp_path / "vault.key").unlink()
    with pytest.raises(RuntimeError, match="密钥"):
        Vault(tmp_path)


def test_rejects_cookie_import_and_path_escape(tmp_path):
    vault = Vault(tmp_path)
    with pytest.raises(ValueError):
        vault.save(cookies()[2:], {"user_agent": "Chrome/155"}, "游客")
    with pytest.raises(KeyError):
        vault.export("../../vault.key")


@pytest.mark.parametrize("remaining_days, expected", [(2, "expiring"), (-1, "expired"), (30, "valid")])
def test_saved_accounts_report_expiry_reminders(tmp_path, remaining_days, expected):
    jar = cookies()
    lifetime = 60 * 86400
    issued = int(datetime.now(UTC).timestamp()) + remaining_days * 86400 - lifetime
    jar[-1]["value"] = f"TEST_GUARD%7C{issued}%7C{lifetime}%7Cdate"
    account = Vault(tmp_path).save(jar, {"user_agent": "Chrome/155"}, "账号")
    assert account["expiry_state"] == expected
