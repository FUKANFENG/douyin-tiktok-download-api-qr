import json

import pytest

from cookie_service.vault import Vault
from tests.test_vault import cookies


def test_browser_state_is_encrypted_and_old_records_still_load(tmp_path):
    vault = Vault(tmp_path)
    old = vault.save(cookies(), {"user_agent": "Chrome/155"}, "shared")
    assert vault.browser_state(old["id"])["origins"] == []
    state = {
        "cookies": cookies(),
        "origins": [
            {
                "origin": "https://creator.douyin.com",
                "localStorage": [{"name": "auth", "value": "LOCAL_STORAGE_SECRET"}],
            }
        ],
    }
    revision = vault.export(old["id"])["updated_at"]
    vault.update_browser_state(old["id"], state, expected_revision=revision)
    assert Vault(tmp_path).browser_state(old["id"]) == state
    assert "LOCAL_STORAGE_SECRET" not in json.dumps(vault.list_accounts())
    assert all(b"LOCAL_STORAGE_SECRET" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_stale_browser_cannot_overwrite_new_scan(tmp_path):
    vault = Vault(tmp_path)
    saved = vault.save(cookies(), {"user_agent": "Chrome/155"}, "shared")
    original = vault.export(saved["id"])
    vault.save(cookies(), {"user_agent": "Chrome/156"}, "renewed", saved["id"])
    with pytest.raises(ValueError, match="changed"):
        vault.update_browser_state(
            saved["id"], {"cookies": cookies(), "origins": []}, expected_revision=original["updated_at"]
        )
    assert vault.export(saved["id"])["label"] == "renewed"


def test_creator_scoped_cookie_does_not_replace_collection_session(tmp_path):
    vault = Vault(tmp_path)
    saved = vault.save(cookies(), {"user_agent": "Chrome/155"}, "shared")
    state = {
        "cookies": cookies()
        + [
            {
                "name": "sessionid",
                "value": "CREATOR_PATH_ONLY",
                "domain": ".douyin.com",
                "path": "/creator-only",
            }
        ],
        "origins": [],
    }
    vault.update_browser_state(saved["id"], state, expected_revision=saved["updated_at"])
    assert "CREATOR_PATH_ONLY" not in vault.export(saved["id"])["cookie_header"]
    assert vault.browser_state(saved["id"])["cookies"][-1]["value"] == "CREATOR_PATH_ONLY"
