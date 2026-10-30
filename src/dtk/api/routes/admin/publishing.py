"""Administrator-only publishing through the existing encrypted account service."""

import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

from dtk.api.deps import Principal
from dtk.api.routes.admin.qr_login import import_account, ok
from dtk.api.routes.openapi import I18N_KEY
from dtk.api.routes.support import admin_only, audit
from dtk.core.errors import InvalidParam, UpstreamUnreachable
from dtk.identity.local_login import Account, LocalId, service_response

router = APIRouter(prefix="/publishing", dependencies=[Depends(admin_only)])
JobId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$", description="Publishing job identifier.")]
MAX_VIDEO_BYTES = 1024 * 1024 * 1024


class TopicItem(BaseModel):
    id: str | None = Field(
        default=None,
        pattern=r"^[0-9]{1,30}$",
        description="Platform topic ID; null for an explicitly selected custom topic.",
    )
    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[^\s#@\x00-\x1f\x7f]+$",
        description="Topic name without a hash or separators.",
    )
    source: Literal["search", "recommend", "new"] = Field(
        default="search",
        description="Whether the topic came from search, video recommendations, or explicit custom input.",
    )

    @model_validator(mode="after")
    def valid_identity(self):
        if (self.source == "new" and self.id is not None) or (
            self.source != "new" and (self.id is None or not self.id.strip("0"))
        ):
            raise ValueError("Select a valid topic or explicitly choose a custom topic")
        return self


class TopicSelection(BaseModel):
    topics: list[TopicItem] = Field(
        default_factory=list,
        max_length=5,
        description="Up to five unique selected topics in the order to insert.",
    )

    @model_validator(mode="after")
    def unique_topics(self):
        ids = [t.id for t in self.topics if t.id is not None]
        if len({t.name.casefold() for t in self.topics}) != len(self.topics) or len(
            set(ids)
        ) != len(ids):
            raise ValueError("Select each topic once")
        return self


class TopicCandidate(TopicItem):
    views: int | None = Field(default=None, ge=0, le=10**18)


class TopicSearch(BaseModel):
    account_id: LocalId = Field(description="Saved account used to query native topic suggestions.")
    keyword: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="Topic search keyword, up to 100 characters.",
    )


class TopicUpdate(TopicSelection):
    revision: LocalId = Field(
        description="Current topic selection version from the job being edited."
    )


class Recommendation(BaseModel):
    confirm_upload: Literal[True] = Field(
        description="Explicit consent to upload this video to a platform draft without publishing."
    )


class NewVideo(TopicSelection):
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
    topics_revision: LocalId | None = Field(
        default=None, description="The topic selection version shown in confirmation."
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
        "recommending",
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
    topics: list[TopicItem] = Field(default_factory=list)
    topics_revision: LocalId = "00000000000000000000000000000000"
    recommended_topics: list[TopicCandidate] = Field(default_factory=list)
    recommendations_loaded: bool = False


async def remote(request: Request, method: str, path: str = "", body=None, *, content=None):
    response = await service_response(
        request,
        method,
        "/api/publishing" + path,
        body,
        content=content,
        timeout=930 if content is not None else 100 if path == "/topics/search" else 20,
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
            "max_topics": 5,
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


@router.post(
    "/topics/search",
    summary="Search native Douyin topics for a saved account",
    openapi_extra={I18N_KEY: "publishing_topic_search"},
)
async def topic_search(request: Request, body: TopicSearch):
    """Return validated platform topic candidates without exposing browser credentials."""
    data = await remote(request, "POST", "/topics/search", body.model_dump())
    try:
        items = [
            TopicCandidate.model_validate(item).model_dump(mode="json")
            for item in data["items"][:20]
        ]
    except (ValidationError, KeyError, TypeError):
        raise UpstreamUnreachable(details={"reason": "topic_search_failed"}) from None
    return ok(request, {"items": items})


@router.put(
    "/jobs/{job_id}/topics",
    summary="Replace a draft's selected topics",
    openapi_extra={I18N_KEY: "publishing_topics"},
)
async def update_topics(request: Request, job_id: JobId, body: TopicUpdate):
    """Require the current selection version to prevent overwriting another editor."""
    return ok(
        request,
        public_job(await remote(request, "PUT", f"/jobs/{job_id}/topics", body.model_dump())),
    )


@router.post(
    "/jobs/{job_id}/recommend",
    summary="Prepare a platform draft and read video topic recommendations",
    openapi_extra={I18N_KEY: "publishing_recommend"},
)
async def recommend(request: Request, job_id: JobId, body: Recommendation):
    """Explicitly uploads to a platform draft, without clicking the publication button."""
    return ok(
        request,
        public_job(await remote(request, "POST", f"/jobs/{job_id}/recommend", body.model_dump())),
    )


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
