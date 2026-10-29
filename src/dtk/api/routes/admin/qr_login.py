"""QR sessions and one-way imports, under the existing identity management permission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from dtk.api.deps import Principal
from dtk.api.routes.openapi import I18N_KEY
from dtk.api.routes.support import audit, manage_pool
from dtk.api.routes.support import ok as envelope_ok
from dtk.core.errors import InvalidParam, UpstreamUnreachable
from dtk.core.types import IdentitySource, IdentityState, Platform
from dtk.db.models import Identity, LocalQrImport
from dtk.identity.importing import build_report, session_expiry
from dtk.identity.local_login import (
    Account,
    ExportRecord,
    LocalId,
    LoginSession,
    service_json,
    service_response,
)
from dtk.identity.pool import IdentityPool

router = APIRouter(prefix="/qr-login", dependencies=[Depends(manage_pool)])
RecordId = Annotated[
    str, Path(pattern=r"^[a-f0-9]{32}$", description="Local QR service record ID.")
]


def ok(request: Request, data: Any, *, status_code: int = 200):
    response = envelope_ok(request, data, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


class StartLogin(BaseModel):
    label: str = Field(default="", max_length=80, description="Optional note for the saved login.")
    replace_id: LocalId | None = Field(
        default=None, description="Saved account to renew, or null for a new login."
    )


class VerificationCode(BaseModel):
    code: str = Field(
        pattern=r"^[0-9]{4,8}$", description="Verification code requested by the platform."
    )


def public_session(data: dict) -> dict:
    try:
        return LoginSession.model_validate(data).model_dump(mode="json")
    except ValidationError:
        raise UpstreamUnreachable(details={"reason": "qr_invalid_response"}) from None


@router.get("", summary="Local QR service status", openapi_extra={I18N_KEY: "qr_overview"})
async def overview(request: Request):
    """List saved login summaries and the latest scan. Never returns credentials."""
    settings = request.app.state.settings
    if not settings.qr_login_url or not settings.qr_login_token:
        return ok(request, {"configured": False, "accounts": [], "session": None})
    data = await service_json(request, "GET", "/api/bootstrap")
    try:
        accounts = [
            Account.model_validate(item).model_dump(mode="json") for item in data["accounts"]
        ]
        latest = public_session(data["session"]) if data.get("session") else None
    except (ValidationError, KeyError, TypeError, AttributeError):
        raise UpstreamUnreachable(details={"reason": "qr_invalid_response"}) from None
    return ok(request, {"configured": True, "accounts": accounts, "session": latest})


@router.post(
    "/sessions",
    status_code=201,
    summary="Start a Douyin QR login",
    openapi_extra={I18N_KEY: "qr_start"},
)
async def start(request: Request, body: StartLogin, principal: Principal = Depends(manage_pool)):
    """Create a local login session. Poll its status, then import the completed account."""
    data = await service_json(request, "POST", "/api/logins", body.model_dump())
    await audit(request, principal, "identity.qr_started")
    return ok(request, public_session(data), status_code=201)


@router.get(
    "/sessions/{session_id}", summary="Read QR login status", openapi_extra={I18N_KEY: "qr_status"}
)
async def status(request: Request, session_id: RecordId):
    """Read scan progress and the saved account summary after confirmation on the phone."""
    return ok(
        request, public_session(await service_json(request, "GET", f"/api/logins/{session_id}"))
    )


@router.get(
    "/sessions/{session_id}/qr",
    summary="Show the login QR image",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}, "description": "The current QR image."}},
    openapi_extra={I18N_KEY: "qr_image"},
)
async def qr(request: Request, session_id: RecordId):
    """Return the current PNG without caching. The same operator permission is required."""
    image = await service_response(request, "GET", f"/api/logins/{session_id}/qr")
    if not image.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UpstreamUnreachable(details={"reason": "qr_invalid_response"})
    return Response(image.content, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.delete(
    "/sessions/{session_id}", summary="Cancel a QR login", openapi_extra={I18N_KEY: "qr_cancel"}
)
async def cancel(request: Request, session_id: RecordId):
    """Cancel the pending scan and close its isolated background browser."""
    return ok(
        request, public_session(await service_json(request, "DELETE", f"/api/logins/{session_id}"))
    )


@router.post(
    "/sessions/{session_id}/code",
    summary="Submit a login verification code",
    openapi_extra={I18N_KEY: "qr_code"},
)
async def code(request: Request, session_id: RecordId, body: VerificationCode):
    """Submit the code only when the platform is waiting for verification. Poll for completion."""
    await service_json(request, "POST", f"/api/logins/{session_id}/code", body.model_dump())
    return ok(request, {"submitted": True})


@router.post(
    "/accounts/{account_id}/import",
    summary="Import a local Douyin login",
    openapi_extra={I18N_KEY: "qr_import"},
)
async def import_account(
    request: Request, account_id: RecordId, principal: Principal = Depends(manage_pool)
):
    """Encrypt the saved login into the identity pool. Repeated imports reuse the identity; renewed logins replace it atomically."""
    db = request.state.db
    # Serialize even the first import across API processes; the transaction owns the lock.
    lock = int.from_bytes(hashlib.sha256(f"qr:{account_id}".encode()).digest()[:8], signed=True)
    await db.execute(select(func.pg_advisory_xact_lock(lock)))
    exported = await service_json(request, "GET", f"/api/accounts/{account_id}/export")
    try:
        saved = ExportRecord.model_validate(exported)
    except ValidationError:
        raise InvalidParam(details={"reason": "qr_invalid_login"}) from None
    if saved.id != account_id or saved.user_agent != saved.fingerprint.user_agent:
        raise InvalidParam(details={"reason": "qr_invalid_login"})
    if saved.expires_at and saved.expires_at <= datetime.now(UTC):
        raise InvalidParam(details={"reason": "qr_expired"})
    report = build_report(
        json.dumps([cookie.model_dump() for cookie in saved.cookies]),
        Platform.DOUYIN,
        user_agent=saved.user_agent,
        language=saved.fingerprint.language,
        timezone=saved.fingerprint.timezone,
    )
    expires_at = session_expiry(report.cookies, recorded=saved.expires_at)
    if expires_at and expires_at <= datetime.now(UTC):
        raise InvalidParam(details={"reason": "qr_expired"})
    if (
        not report.usable
        or not report.authenticated
        or not report.cookies.get("sid_tt")
        or not (report.cookies.get("sessionid") or report.cookies.get("sessionid_ss"))
    ):
        raise InvalidParam(details={"reason": "qr_invalid_login"})
    screen = saved.fingerprint.screen
    fingerprint = replace(
        report.fingerprint,
        platform=saved.fingerprint.platform,
        screen=f"{screen.width}x{screen.height}" if screen else None,
        hardware_concurrency=saved.fingerprint.hardware_concurrency,
        device_memory=saved.fingerprint.device_memory,
    )
    link = await db.get(LocalQrImport, account_id)
    previous = await db.get(Identity, link.identity_id) if link else None
    if (
        link
        and previous
        and previous.state != IdentityState.RETIRED.value
        and link.source_updated_at >= saved.updated_at
    ):
        if link.source_updated_at == saved.updated_at:
            previous.session_expires_at = session_expiry(
                report.cookies,
                recorded=min(
                    (
                        value
                        for value in (previous.session_expires_at, saved.expires_at)
                        if value is not None
                    ),
                    default=None,
                ),
            )
        return ok(request, {"stored": False, "identity_id": str(previous.id)})

    pool = IdentityPool(request.app.state.cipher)
    async with db.begin_nested():
        identity_id = await pool.add(
            db,
            platform=Platform.DOUYIN,
            cookies=report.cookies,
            fingerprint=fingerprint,
            source=IdentitySource.IMPORTED,
            authenticated=True,
            session_expires_at=expires_at,
            note=previous.note if previous and previous.note else saved.label or "Douyin QR login",
        )
        if previous and previous.state != IdentityState.RETIRED.value:
            new = await db.get(Identity, identity_id)
            new.rotation_endpoints = previous.rotation_endpoints
            await pool.retire(db, str(previous.id), "local_qr_refreshed")
        if link is None:
            link = LocalQrImport(account_id=account_id)
            db.add(link)
        link.identity_id = identity_id
        link.source_updated_at = saved.updated_at
        await audit(
            request,
            principal,
            "identity.qr_imported",
            target_type="identity",
            target_id=str(identity_id),
        )
        await db.flush()
    return ok(request, {"stored": True, "identity_id": str(identity_id)})
