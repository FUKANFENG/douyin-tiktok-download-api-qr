import contextlib
import json
import time
from urllib.parse import urlsplit

import pytest

from cookie_service.browser import find_browser
from cookie_service.publisher import USER_PATH, WORK_PATH, DouyinPublisher
from cookie_service.publishing import PublishProblem
from tests.test_publishing import VIDEO

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not find_browser(), reason="No Chromium")]


def fixture_publisher(sent, *, wrong_id=False):
    class FixturePublisher(DouyinPublisher):
        @contextlib.asynccontextmanager
        async def session(self, record):
            async with super().session(record) as (context, page):

                async def route(route):
                    path = urlsplit(route.request.url).path
                    if path.rstrip("/") == USER_PATH.rstrip("/"):
                        return await route.fulfill(
                            json={"status_code": 0, "user": {"uid": "42", "nickname": "Creator"}}
                        )
                    if path.rstrip("/") == WORK_PATH.rstrip("/"):
                        return await route.fulfill(
                            json={
                                "works": [
                                    {"aweme_id": "101", "title": "Title", "create_time": int(time.time())}
                                ]
                                if sent
                                else []
                            }
                        )
                    if path == "/aweme/v1/search/challengesug/":
                        return await route.fulfill(
                            json={
                                "status_code": 0,
                                "sug_list": [{"cid": "123", "cha_name": "美食", "view_count": 99}],
                            }
                        )
                    if path == "/web/api/media/hashtag/rec/":
                        return await route.fulfill(
                            json={
                                "status_code": 0,
                                "challenge_list": [{"cid": "123", "cha_name": "美食", "view_count": 99}],
                            }
                        )
                    if path == "/web/api/media/aweme/create_v2/":
                        sent.append(route.request.post_data_json)
                        return await route.fulfill(json={"status_code": 0})
                    if path.endswith("/content/upload"):
                        html = f'<input type="file" onchange="location.href=\'/creator-micro/content/post/video\'"><script>fetch({json.dumps(USER_PATH)})</script>'
                    elif path.endswith("/content/post/video"):
                        # Native widgets, a search candidate and a structured POST are all exercised.
                        html = """<input placeholder="填写作品标题"><div class="zone-container" contenteditable="true"></div>
                        <div class="long-card-test"><div>重新上传</div></div>
                        <div class="tag-hash-fixture" onclick="choose()"><span>美食</span></div>
                        <button onclick="publish()">发布</button><script>
                        fetch('/web/api/media/hashtag/rec/');
                        const editor=document.querySelector('.zone-container');let chosen=false;
                        function choose(){chosen=true;editor.textContent='Description ';
                          const chip=document.createElement('span');chip.dataset.mention='#';chip.textContent='#美食';editor.append(chip);}
                        function publish(){const text=editor.textContent;
                          const item={start:text.indexOf('#'),end:text.length,type:1,hashtag_id:ID,hashtag_name:'美食'};
                          fetch('/web/api/media/aweme/create_v2/',{method:'POST',headers:{'Content-Type':'application/json'},
                          body:JSON.stringify({common:{text,text_extra:chosen?[item]:[],challenges:[String(ID)]}})})
                          .then(()=>location.href='/creator-micro/content/manage').catch(()=>{});}
                        </script>""".replace("ID", "999" if wrong_id else "123")
                    elif path.endswith("/content/manage"):
                        html = f"<script>fetch({json.dumps(WORK_PATH)})</script>"
                    else:
                        return await route.abort()
                    await route.fulfill(content_type="text/html; charset=utf-8", body=html)

                await context.route("**/*", route)
                yield context, page

    return FixturePublisher()


def record():
    return {"storage_state": {"cookies": [], "origins": []}, "user_agent": "Chrome/155"}


def job():
    return {
        "account_uid": "42",
        "title": "Title",
        "description": "Description",
        "topics": [{"id": "123", "name": "美食", "source": "search"}],
    }


async def test_native_search_and_video_recommendations_do_not_publish(tmp_path):
    sent = []
    driver = fixture_publisher(sent)
    results = await driver.search_topics(record(), "美食")
    assert results[0]["id"] == "123"
    path = tmp_path / "video.mp4"
    path.write_bytes(VIDEO)
    result = await driver.recommend(record(), job(), path)
    assert result["recommendations"][0]["source"] == "recommend"
    assert sent == []


@pytest.mark.parametrize("wrong_id", [False, True])
async def test_outgoing_native_topic_id_is_checked_before_any_submission(tmp_path, wrong_id):
    sent = []
    driver = fixture_publisher(sent, wrong_id=wrong_id)
    path = tmp_path / "video.mp4"
    path.write_bytes(VIDEO)
    if wrong_id:
        with pytest.raises(PublishProblem, match="topics_unverified") as caught:
            await driver.publish(record(), job(), path, lambda: None)
        assert caught.value.submission_blocked is True
        assert sent == []
    else:
        result = await driver.publish(record(), job(), path, lambda: None)
        assert result["post_id"] == "101"
        assert len(sent) == 1
