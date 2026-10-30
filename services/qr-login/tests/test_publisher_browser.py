"""Exercise the real browser adapter against a local, intercepted creator UI."""

import contextlib
import json
import time
from urllib.parse import urlsplit

import pytest

from cookie_service.browser import find_browser
from cookie_service.publisher import USER_PATH, WORK_PATH, DouyinPublisher
from tests.test_publishing import VIDEO


@pytest.mark.skipif(not find_browser(), reason="No local Chromium for browser contract test")
@pytest.mark.asyncio
async def test_real_browser_submits_once_and_verifies_a_new_work(tmp_path):
    submitted = []
    old = {"aweme_id": "100", "title": "Same title", "create_time": int(time.time())}
    new = {"aweme_id": "101", "title": "Same title", "create_time": int(time.time())}

    class FixturePublisher(DouyinPublisher):
        @contextlib.asynccontextmanager
        async def session(self, record):
            async with super().session(record) as (context, page):

                async def route(request):
                    path = urlsplit(request.request.url).path
                    if path.rstrip("/") == USER_PATH.rstrip("/"):
                        return await request.fulfill(
                            json={"status_code": 0, "user": {"uid": "42", "nickname": "Fixture Creator"}}
                        )
                    if path.rstrip("/") == WORK_PATH.rstrip("/"):
                        return await request.fulfill(json={"works": [old, new] if submitted else [old]})
                    if path == "/fixture-submit":
                        submitted.append(1)
                        return await request.fulfill(json={"ok": True})
                    if path.endswith("/content/upload"):
                        html = f'<input type="file" onchange="location.href=\'/creator-micro/content/post/video\'"><script>fetch({json.dumps(USER_PATH)})</script>'
                    elif path.endswith("/content/post/video"):
                        html = """<input placeholder="填写作品标题"><div class="zone-container" contenteditable="true"></div><div class="long-card-test"><div>重新上传</div></div><button onclick="fetch('/fixture-submit',{method:'POST'}).then(()=>location.href='/creator-micro/content/manage')">发布</button>"""
                    elif path.endswith("/content/manage"):
                        html = f"<h1>作品管理</h1><script>fetch({json.dumps(WORK_PATH)})</script>"
                    else:
                        return await request.abort()
                    await request.fulfill(content_type="text/html; charset=utf-8", body=html)

                await context.route("**/*", route)
                yield context, page

    publisher = FixturePublisher()
    record = {"storage_state": {"cookies": [], "origins": []}, "user_agent": "Mozilla/5.0 Chrome/155.0.0.0"}
    checked = await publisher.check(record)
    assert checked["uid"] == "42"
    assert submitted == []
    video = tmp_path / "fixture.mp4"
    video.write_bytes(VIDEO)
    boundaries = []
    result = await publisher.publish(
        record,
        {"account_uid": "42", "title": "Same title", "description": "Description"},
        video,
        lambda: boundaries.append(1),
    )
    assert result["post_id"] == "101"
    assert boundaries == [1]
    assert submitted == [1]
