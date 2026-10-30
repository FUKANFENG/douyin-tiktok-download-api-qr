from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .publishing import MAX_VIDEO_BYTES, NewJob

router = APIRouter(prefix="/api/publishing")


class Confirmation(BaseModel):
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirm: Literal[True]


def public(job):
    return {key: value for key, value in job.items() if key not in {"client_key", "account_revision"}}


@router.get("")
async def overview(request: Request):
    manager = request.app.state.publishing
    return {
        "accounts": manager.vault.list_accounts(),
        "items": [public(job) for job in manager.list(limit=100)],
        "max_video_bytes": MAX_VIDEO_BYTES,
    }


@router.post("/jobs", status_code=201)
async def create(request: Request, body: NewJob):
    return public(request.app.state.publishing.create(body.model_dump()))


@router.get("/jobs/{job_id}")
async def status(request: Request, job_id: str):
    return public(request.app.state.publishing.get(job_id))


@router.put("/jobs/{job_id}/video")
async def video(request: Request, job_id: str):
    return public(await request.app.state.publishing.receive(job_id, request.stream()))


@router.post("/jobs/{job_id}/prepare")
async def prepare(request: Request, job_id: str):
    return public(request.app.state.publishing.prepare(job_id))


@router.post("/jobs/{job_id}/publish")
async def publish(request: Request, job_id: str, body: Confirmation):
    return public(request.app.state.publishing.submit(job_id, body.account_id))


@router.delete("/jobs/{job_id}")
async def cancel(request: Request, job_id: str):
    return public(request.app.state.publishing.cancel(job_id))
