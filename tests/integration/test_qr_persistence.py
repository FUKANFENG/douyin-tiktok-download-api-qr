"""QR login expiry and identity links survive the persistence paths users rely on."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select

from dtk.core.db import session_scope
from dtk.db.models import Identity, LocalQrImport
from dtk.ops import backup
from dtk.worker.maintenance import Maintenance, MaintenanceReport
from tests.integration import test_api_qr_login as qr
from tests.integration import test_api_support as support

api_app = support.api_app
client = support.client
remote = qr.remote
pytestmark = pytest.mark.integration


async def import_saved(client):
    response = await client.post(f"{qr.BASE}/accounts/{qr.ACCOUNT}/import")
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def listed_expiry(client, identity_id):
    rows = (await client.get("/api/v1/admin/identities")).json()["data"]
    value = next(row for row in rows if row["id"] == identity_id)["expires_at"]
    return datetime.fromisoformat(value) if value else None


async def test_browser_expiry_reaches_the_list_and_expiry_reminder(client, api_app, remote):
    await support.signed_in(client)
    expiry = datetime.now(UTC) + timedelta(days=1)
    remote["expires_at"] = expiry.isoformat()
    for cookie in remote["cookies"]:
        cookie["expires"] = expiry.timestamp()
    imported = await import_saved(client)
    assert await listed_expiry(client, imported["identity_id"]) == expiry
    maintenance = Maintenance(cipher=api_app.state.cipher, config=api_app.state.config)
    report = MaintenanceReport()
    await maintenance.warn_expiring_sessions(report)
    assert report.expiring_sessions == 1


@pytest.mark.parametrize("source_days,cookie_days", [(1, 5), (5, 1)])
async def test_the_earliest_known_expiry_wins(client, remote, source_days, cookie_days):
    await support.signed_in(client)
    issued = int(datetime.now(UTC).timestamp())
    source_expiry = datetime.fromtimestamp(issued + source_days * 86400, UTC)
    cookie_expiry = datetime.fromtimestamp(issued + cookie_days * 86400, UTC)
    remote["expires_at"] = source_expiry.isoformat()
    remote["cookies"].append(
        {"name": "sid_guard", "value": f"fixture|{issued}|{cookie_days * 86400}|fixture"}
    )
    imported = await import_saved(client)
    assert await listed_expiry(client, imported["identity_id"]) == min(source_expiry, cookie_expiry)


async def test_reimport_backfills_expiry_without_replacing_existing_identity(client, remote):
    await support.signed_in(client)
    first = await import_saved(client)
    async with session_scope() as db:
        row = await db.get(Identity, UUID(first["identity_id"]))
        row.session_expires_at = None
    second = await import_saved(client)
    assert second == {"stored": False, "identity_id": first["identity_id"]}
    assert await listed_expiry(client, first["identity_id"]) == datetime.fromisoformat(
        remote["expires_at"]
    )


async def test_an_expired_cookie_deadline_is_not_overridden_by_a_later_source_date(client, remote):
    await support.signed_in(client)
    issued = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    remote["cookies"].append({"name": "sid_guard", "value": f"fixture|{issued}|86400|fixture"})
    response = await client.post(f"{qr.BASE}/accounts/{qr.ACCOUNT}/import")
    assert response.status_code == 400
    assert response.json()["error"]["details"]["reason"] == "qr_expired"


async def test_backup_restore_keeps_deduplication_expiry_and_renewal(client, remote, tmp_path):
    await support.signed_in(client)
    first = await import_saved(client)
    async with session_scope() as db:
        row = await db.get(Identity, UUID(first["identity_id"]))
        row.rotation_endpoints = []
    async with session_scope() as db:
        info = await backup.create_backup(
            db, output=tmp_path, secret_key=support.TEST_SECRET_KEY, include_identities=True
        )
    assert info.manifest.contents["local_qr_imports"] == 1
    async with session_scope() as db:
        await db.execute(delete(Identity))
    async with session_scope() as db:
        await backup.restore_backup(db, info.path, secret_key=support.TEST_SECRET_KEY)
    second = await import_saved(client)
    assert second == {"stored": False, "identity_id": first["identity_id"]}
    assert await listed_expiry(client, first["identity_id"]) == datetime.fromisoformat(
        remote["expires_at"]
    )
    remote["updated_at"] = datetime.now(UTC).isoformat()
    remote["cookies"][1]["value"] += "-renewed"
    renewed = await import_saved(client)
    assert renewed["identity_id"] != first["identity_id"]
    async with session_scope() as db:
        old = await db.get(Identity, UUID(first["identity_id"]))
        new = await db.get(Identity, UUID(renewed["identity_id"]))
        assert old.state == "retired" and old.cookies_encrypted == b""
        assert new.rotation_endpoints == []
        active = await db.scalar(
            select(func.count()).select_from(Identity).where(Identity.state == "active")
        )
        assert active == 1


async def test_backup_without_identities_excludes_account_links(client, remote, tmp_path):
    await support.signed_in(client)
    await import_saved(client)
    async with session_scope() as db:
        info = await backup.create_backup(db, output=tmp_path, secret_key=support.TEST_SECRET_KEY)
    assert "identities" not in info.manifest.contents
    assert "local_qr_imports" not in info.manifest.contents


async def test_a_legacy_archive_without_qr_metadata_still_restores(client, remote, tmp_path):
    await support.signed_in(client)
    first = await import_saved(client)
    async with session_scope() as db:
        info = await backup.create_backup(
            db, output=tmp_path / "new", secret_key=support.TEST_SECRET_KEY, include_identities=True
        )
    tables = {
        name: backup.read_table(info.path, name)
        for name in info.manifest.contents
        if name != "local_qr_imports"
    }
    for row in tables["identities"]:
        row.pop("session_expires_at", None)
    manifest = replace(
        info.manifest, schema_version=2, contents={name: len(rows) for name, rows in tables.items()}
    )
    legacy = backup.write_archive(tmp_path / "legacy.tar.gz", manifest, tables)
    async with session_scope() as db:
        await db.execute(delete(Identity))
    async with session_scope() as db:
        await backup.restore_backup(db, legacy, secret_key=support.TEST_SECRET_KEY)
    async with session_scope() as db:
        row = await db.get(Identity, UUID(first["identity_id"]))
        assert row is not None and row.session_expires_at is None
        assert await db.get(LocalQrImport, qr.ACCOUNT) is None


async def test_identity_export_and_bundle_import_preserve_browser_expiry(client, remote):
    await support.signed_in(client)
    first = await import_saved(client)
    response = await client.post(
        "/api/v1/admin/identities/export", json={"identity_ids": [first["identity_id"]]}
    )
    document = response.json()["data"]
    restored = await client.post(
        "/api/v1/admin/identities/import/bundle",
        json={"version": document["version"], "identities": document["identities"]},
    )
    assert restored.status_code == 201, restored.text
    data = restored.json()["data"]
    row = data["results"][0]
    assert await listed_expiry(client, row["identity_id"]) == datetime.fromisoformat(
        remote["expires_at"]
    )


async def test_invalid_expiry_in_a_bundle_does_not_partially_import(client, remote):
    await support.signed_in(client)
    first = await import_saved(client)
    document = (
        await client.post(
            "/api/v1/admin/identities/export", json={"identity_ids": [first["identity_id"]]}
        )
    ).json()["data"]
    invalid = deepcopy(document["identities"][0])
    invalid["session_expires_at"] = "not-a-date"
    response = await client.post(
        "/api/v1/admin/identities/import/bundle",
        json={"version": document["version"], "identities": [document["identities"][0], invalid]},
    )
    assert response.status_code == 400
    async with session_scope() as db:
        assert await db.scalar(select(func.count()).select_from(Identity)) == 1
