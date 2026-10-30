"""Administrator-only publishing through the existing encrypted account service."""

import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field, ValidationError

from dtk.api.deps import Principal
from dtk.api.routes.admin.qr_login import import_account, ok
from dtk.api.routes.openapi import I18N_KEY
from dtk.api.routes.support import admin_only, audit
from dtk.core.errors import InvalidParam, UpstreamUnreachable
from dtk.identity.local_login import Account, LocalId, service_response

router = APIRouter(prefix="/publishing", dependencies=[Depends(admin_only)])
JobId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$", description="Publishing job identifier.")]
MAX_VIDEO_BYTES = 1024 * 1024 * 1024


class NewVideo(BaseModel):
    account_id: LocalId = Field(description="Saved QR account to publish from.")
    client_key: LocalId = Field(
        description="Random 32-character hexadecimal idempotency key for this draft."
    )
    title: str = Field(min_length=1, max_length=30, description="Video title, up to 30 characters.")
    description: str = Field(default="", max_length=1000, description="Optional video description.")
    filename: str = Field(
        min_length=1, max_length=180, description="MP4, MOV or M4V filename without a directory."
    )
    size: int = Field(
        ge=16, le=MAX_VIDEO_BYTES, description="Exact number of bytes to upload, up to 1 GiB."
    )


class Confirmation(BaseModel):
    account_id: LocalId = Field(description="The verified account ID shown for this job.")
    confirm: Literal[True] = Field(
        description="Explicit confirmation to upload and publish the prepared video."
    )


class Job(BaseModel):
    id: LocalId
    account_id: LocalId
    title: str
    description: str
    filename: str
    size: int
    state: Literal[
        "awaiting_upload",
        "receiving",
        "uploaded",
        "checking",
        "ready",
        "uploading",
        "submitting",
        "published",
        "failed",
        "uncertain",
        "cancelled",
    ]
    account_label: str
    account_name: str
    account_uid: str
    error_code: str | None = None
    post_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    created_at: str
    updated_at: str


async def remote(request: Request, method: str, path: str = "", body=None, *, content=None):
    response = await service_response(
        request,
        method,
        "/api/publishing" + path,
        body,
        content=content,
        timeout=930 if content is not None else 20,
        accept_errors=True,
    )
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise UpstreamUnreachable(details={"reason": "publishing_unavailable"}) from None
    if response.status_code >= 300:
        code = data.get("detail") if isinstance(data, dict) else None
        reason = (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code)
            else "publishing_unavailable"
        )
        raise InvalidParam(details={"reason": reason})
    return data


def public_job(data):
    try:
        return Job.model_validate(data).model_dump(mode="json")
    except ValidationError:
        raise UpstreamUnreachable(details={"reason": "publishing_unavailable"}) from None


@router.get(
    "",
    summary="List shared accounts and video publishing jobs",
    openapi_extra={I18N_KEY: "publishing_overview"},
)
async def overview(request: Request):
    """Account summaries and recent jobs; no browser credentials leave the server."""
    settings = request.app.state.settings
    if not settings.qr_login_url or not settings.qr_login_token:
        return ok(
            request,
            {"configured": False, "accounts": [], "items": [], "max_video_bytes": MAX_VIDEO_BYTES},
        )
    data = await remote(request, "GET")
    try:
        accounts = [
            Account.model_validate(item).model_dump(mode="json") for item in data["accounts"]
        ]
        jobs = [public_job(item) for item in data["items"][:100]]
    except (ValidationError, KeyError, TypeError):
        raise UpstreamUnreachable(details={"reason": "publishing_unavailable"}) from None
    return ok(
        request,
        {
            "configured": True,
            "accounts": accounts,
            "items": jobs,
            "max_video_bytes": MAX_VIDEO_BYTES,
        },
    )


@router.post(
    "/jobs",
    status_code=201,
    summary="Prepare a local video upload",
    openapi_extra={I18N_KEY: "publishing_create"},
)
async def create(request: Request, body: NewVideo, principal: Principal = Depends(admin_only)):
    """Reserve a draft; the client key makes repeated creation idempotent. Does not publish."""
    job = public_job(await remote(request, "POST", "/jobs", body.model_dump()))
    await audit(
        request,
        principal,
        "publishing.draft_created",
        target_type="publishing_job",
        target_id=job["id"],
    )
    return ok(request, job, status_code=201)


@router.get(
    "/jobs/{job_id}",
    summary="Read a video publishing job",
    openapi_extra={I18N_KEY: "publishing_status"},
)
async def status(request: Request, job_id: JobId):
    """Read durable progress, including an uncertain outcome that must not be retried."""
    return ok(request, public_job(await remote(request, "GET", f"/jobs/{job_id}")))


@router.put(
    "/jobs/{job_id}/video",
    summary="Stream a video to the local publishing service",
    openapi_extra={
        I18N_KEY: "publishing_video",
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
    },
)
async def video(request: Request, job_id: JobId):
    """Stream raw MP4/MOV bytes without buffering the video in API memory."""
    return ok(
        request,
        public_job(await remote(request, "PUT", f"/jobs/{job_id}/video", content=request.stream())),
    )


@router.post(
    "/jobs/{job_id}/prepare",
    summary="Verify the selected creator account",
    openapi_extra={I18N_KEY: "publishing_prepare"},
)
async def prepare(request: Request, job_id: JobId):
    """Check the browser login and resolve the account name before publication is confirmed."""
    return ok(request, public_job(await remote(request, "POST", f"/jobs/{job_id}/prepare")))


@router.post(
    "/jobs/{job_id}/publish",
    summary="Confirm and submit the video once",
    openapi_extra={I18N_KEY: "publishing_publish"},
)
async def publish(
    request: Request, job_id: JobId, body: Confirmation, principal: Principal = Depends(admin_only)
):
    """Requires explicit confirmation and the checked account ID. Never rotates accounts or retries the final click."""
    job = public_job(await remote(request, "POST", f"/jobs/{job_id}/publish", body.model_dump()))
    await audit(
        request,
        principal,
        "publishing.submitted",
        target_type="publishing_job",
        target_id=job_id,
        detail={"account_id": body.account_id},
    )
    return ok(request, job)


@router.post(
    "/jobs/{job_id}/sync",
    summary="Synchronize the shared login into the collection identity",
    openapi_extra={I18N_KEY: "publishing_sync"},
)
async def sync(request: Request, job_id: JobId, principal: Principal = Depends(admin_only)):
    """Refresh the collection identity from the same encrypted browser login after checking or publishing."""
    job = public_job(await remote(request, "GET", f"/jobs/{job_id}"))
    if job["state"] not in {"ready", "published"}:
        raise InvalidParam(details={"reason": "not_ready"})
    result = await import_account(request, job["account_id"], principal)
    return ok(request, json.loads(result.body)["data"])


@router.delete(
    "/jobs/{job_id}",
    summary="Discard a draft and remove its local video",
    openapi_extra={I18N_KEY: "publishing_cancel"},
)
async def cancel(request: Request, job_id: JobId):
    """Remove local media only. A submitted work is never deleted from the platform here."""
    return ok(request, public_job(await remote(request, "DELETE", f"/jobs/{job_id}")))
