"""Bounded, authenticated calls to the optional local QR service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from dtk.core.errors import InvalidParam, NotConfigured, NotFound, UpstreamUnreachable

LocalId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class Account(BaseModel):
    id: LocalId
    label: str = Field(max_length=80)
    platform: Literal["douyin"]
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    cookie_count: int = Field(ge=0, le=1000)
    expiry_state: Literal["unknown", "expired", "expiring", "valid"]


class LoginSession(BaseModel):
    id: LocalId
    status: Literal[
        "starting",
        "waiting",
        "scanned",
        "verification_required",
        "complete",
        "cancelled",
        "expired",
        "error",
    ]
    expires_at: AwareDatetime
    qr_available: bool
    image_revision: int
    account: Account | None = None


class Cookie(BaseModel):
    name: str = Field(min_length=1, max_length=256, pattern=r"^[^;=\s]+$")
    value: str = Field(max_length=32768, pattern=r"^[^\r\n;]*$")


class Screen(BaseModel):
    width: int = Field(gt=0, le=32768)
    height: int = Field(gt=0, le=32768)


class BrowserInfo(BaseModel):
    user_agent: str = Field(min_length=1, max_length=1024)
    platform: str | None = Field(default=None, max_length=80)
    screen: Screen | None = None
    language: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=80)
    hardware_concurrency: int | None = Field(default=None, gt=0, le=1024)
    device_memory: int | None = Field(default=None, gt=0, le=1024)


class ExportRecord(BaseModel):
    version: Literal[1]
    id: LocalId
    platform: Literal["douyin"]
    label: str = Field(max_length=80)
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    cookies: list[Cookie] = Field(min_length=1, max_length=1000)
    user_agent: str = Field(min_length=1, max_length=1024)
    fingerprint: BrowserInfo

    @field_validator("cookies", mode="before")
    @classmethod
    def discard_unnamed_cookie(cls, value: Any) -> Any:
        # Chromium can export a nameless cookie. The local service's Cookie header
        # already omits it; it is not part of the authenticated session.
        if isinstance(value, list):
            return [
                item for item in value if not (isinstance(item, dict) and item.get("name") == "")
            ]
        return value


async def service_response(
    request: Request,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    content: AsyncIterable[bytes] | None = None,
    timeout: float = 12,
    accept_errors: bool = False,
) -> httpx.Response:
    settings = request.app.state.settings
    if not settings.qr_login_url or not settings.qr_login_token:
        raise NotConfigured(details={"reason": "qr_not_configured"})
    try:
        url = urlsplit(settings.qr_login_url)
        port = url.port
        allowed = {"127.0.0.1", "localhost", "::1", "host.docker.internal", "qr-login"}
        if (
            url.scheme != "http"
            or url.hostname not in allowed
            or not port
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError
    except ValueError:
        raise NotConfigured(details={"reason": "qr_invalid_address"}) from None

    # Ignore system proxies and redirects: the bearer token must stay on this local service.
    # https://www.python-httpx.org/environment_variables/
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=timeout
    ) as client:
        try:
            async with client.stream(
                method,
                settings.qr_login_url.rstrip("/") + path,
                headers={
                    "Authorization": f"Bearer {settings.qr_login_token}",
                    "Host": f"127.0.0.1:{port}",
                },
                json=body,
                content=content,
            ) as response:
                if response.status_code == 404 and not accept_errors:
                    raise NotFound(details={"reason": "qr_record_missing"})
                if response.status_code == 409 and not accept_errors:
                    raise InvalidParam(details={"reason": "qr_busy"})
                if response.status_code >= 300 and not accept_errors:
                    raise UpstreamUnreachable(details={"reason": "qr_service_unavailable"})
                response_content = bytearray()
                async for chunk in response.aiter_bytes():
                    response_content.extend(chunk)
                    if len(response_content) > 2 * 1024 * 1024:
                        raise UpstreamUnreachable(details={"reason": "qr_invalid_response"})
                return httpx.Response(
                    response.status_code, content=bytes(response_content), headers=response.headers
                )
        except httpx.HTTPError:
            raise UpstreamUnreachable(details={"reason": "qr_service_unavailable"}) from None


async def service_json(request: Request, method: str, path: str, body: dict | None = None) -> Any:
    response = await service_response(request, method, path, body)
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeError):
        raise UpstreamUnreachable(details={"reason": "qr_invalid_response"}) from None
