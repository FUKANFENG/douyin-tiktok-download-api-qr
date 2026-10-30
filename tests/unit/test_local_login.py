"""A local bridge must never forward credentials through redirects or environment proxies."""

from types import SimpleNamespace

import httpx
import pytest

from dtk.core.errors import InvalidParam, NotConfigured, NotFound, UpstreamUnreachable
from dtk.identity.local_login import service_json, service_response


def request(url="http://host.docker.internal:8790"):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    qr_login_url=url,
                    qr_login_token="test-local-service-token",
                )
            )
        )
    )


@pytest.fixture
def remote(monkeypatch):
    original = httpx.AsyncClient
    calls = []
    result = {"status": 200, "content": b'{"ok":true}', "headers": {}}

    def handler(req):
        calls.append(req)
        return httpx.Response(
            result["status"], content=result["content"], headers=result["headers"]
        )

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return calls, result


async def test_local_request_keeps_token_server_side_and_bypasses_proxies(remote):
    calls, _ = remote
    assert await service_json(request(), "GET", "/api/bootstrap") == {"ok": True}
    assert calls[0].headers["host"] == "127.0.0.1:8790"
    assert calls[0].headers["authorization"] == "Bearer test-local-service-token"
    assert calls[0].url.host == "host.docker.internal"


async def test_docker_service_address_is_supported(remote):
    assert await service_json(request("http://qr-login:8790"), "GET", "/api/bootstrap") == {
        "ok": True
    }
    assert remote[0][0].url.host == "qr-login"
    assert remote[0][0].headers["host"] == "127.0.0.1:8790"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8790",
        "http://127.0.0.1:8790/path",
        "http://user:password@localhost:8790",
        "http://localhost:8790?x=1",
        "http://localhost:bad",
        "http://localhost",
        "https://localhost:8790",
    ],
)
async def test_invalid_destination_is_refused_before_sending(url, remote):
    with pytest.raises(NotConfigured):
        await service_json(request(url), "GET", "/api/bootstrap")
    assert remote[0] == []


@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/api/bootstrap",
        "//example.com/api/bootstrap",
        "/api/publishing/jobs/../../accounts/secret/export",
        "/api/publishing/jobs/%2e%2e/%2e%2e/accounts/secret/export",
        "/api/publishing/jobs/" + "a" * 32 + "?redirect=https://example.com",
        "/api/publishing/jobs/" + "a" * 32 + "#ignored",
        "/api/publishing/jobs/" + "a" * 32 + "\\video",
        "/api/publishing/jobs/" + "a" * 32 + "\n",
        "/healthz",
    ],
)
async def test_untrusted_service_paths_are_refused_before_sending(path, remote):
    with pytest.raises(InvalidParam):
        await service_response(request(), "GET", path, accept_errors=True)
    assert remote[0] == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/bootstrap",
        "/api/logins",
        "/api/logins/" + "a" * 32,
        "/api/logins/" + "a" * 32 + "/qr",
        "/api/logins/" + "a" * 32 + "/code",
        "/api/accounts/" + "a" * 32 + "/export",
        "/api/publishing",
        "/api/publishing/jobs",
        "/api/publishing/jobs/" + "a" * 32,
        "/api/publishing/jobs/" + "a" * 32 + "/video",
        "/api/publishing/jobs/" + "a" * 32 + "/prepare",
        "/api/publishing/jobs/" + "a" * 32 + "/publish",
    ],
)
async def test_qr_and_publishing_paths_stay_on_the_configured_service(path, remote):
    await service_response(request(), "GET", path)
    assert len(remote[0]) == 1
    assert str(remote[0][0].url) == "http://host.docker.internal:8790" + path


@pytest.mark.parametrize(
    "status,error",
    [
        (302, UpstreamUnreachable),
        (401, UpstreamUnreachable),
        (404, NotFound),
        (409, InvalidParam),
        (500, UpstreamUnreachable),
    ],
)
async def test_error_bodies_and_redirects_are_not_exposed(status, error, remote):
    calls, result = remote
    result.update(
        status=status,
        content=b"sensitive upstream failure",
        headers={"Location": "https://example.com/"},
    )
    with pytest.raises(error) as caught:
        await service_json(request(), "GET", "/api/bootstrap")
    assert "sensitive" not in str(caught.value)
    assert len(calls) == 1


async def test_invalid_json_is_a_sanitized_error(remote):
    remote[1]["content"] = b"private-invalid-json"
    with pytest.raises(UpstreamUnreachable):
        await service_json(request(), "GET", "/api/bootstrap")


async def test_oversized_response_is_refused(remote):
    remote[1]["content"] = b"x" * (2 * 1024 * 1024 + 1)
    with pytest.raises(UpstreamUnreachable):
        await service_response(request(), "GET", "/api/bootstrap")
