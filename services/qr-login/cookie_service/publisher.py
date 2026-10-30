"""Douyin video workflow adapted from dreammis/social-auto-upload (MIT).

See third_party/social-auto-upload-LICENSE and UPSTREAM.md. This adapter shares
the encrypted login source, bounds every wait and never repeats the final click.
"""

import asyncio
import contextlib
import os
import re
import time
from urllib.parse import urlsplit

from .browser import find_browser
from .publishing import PublishProblem
from .topics import Topics, candidates, verify_submission

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
USER_PATH = "/web/api/media/user/info/"
WORK_PATH = "/janus/douyin/creator/pc/work_list"
RECOMMEND_PATH = "/web/api/media/hashtag/rec/"
PUBLISH_PATHS = {"/web/api/media/aweme/create", "/web/api/media/aweme/create_v2"}


def response_at(response, path):
    parsed = urlsplit(response.url)
    return parsed.hostname == "creator.douyin.com" and parsed.path.rstrip("/") == path.rstrip("/")


def posts_from(value):
    """Only retain evidence needed to identify a newly created work, never raw responses."""
    if isinstance(value, dict):
        identifier = str(value.get("aweme_id", ""))
        if identifier.isdigit():
            yield {
                "id": identifier,
                "title": value.get("title", ""),
                "desc": value.get("desc", ""),
                "created": value.get("create_time", 0),
            }
        for child in value.values():
            yield from posts_from(child)
    elif isinstance(value, list):
        for child in value:
            yield from posts_from(child)


class DouyinPublisher:
    @contextlib.asynccontextmanager
    async def session(self, record):
        # Kept separate from the QR driver's Playwright import. Patchright is
        # the engine used by the locally verified upstream publishing flow.
        from patchright.async_api import async_playwright

        executable = find_browser()
        if not executable:
            raise PublishProblem("browser_missing")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=executable,
                headless=True,
                chromium_sandbox=os.environ.get("DY_COOKIE_CHROMIUM_SANDBOX", "false").lower() == "true",
            )
            try:
                context = await browser.new_context(
                    storage_state=record["storage_state"],
                    user_agent=record["user_agent"],
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    service_workers="block",
                )
                page = await context.new_page()
                page.set_default_timeout(15000)
                yield context, page
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.close(), timeout=10)

    async def open_upload(self, page):
        async with page.expect_response(lambda r: response_at(r, USER_PATH), timeout=60000) as info:
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
        response = await info.value
        data = await response.json()
        user = data.get("user", {})
        uid, nickname = str(user.get("uid", "")), user.get("nickname", "")
        if (
            response.status != 200
            or data.get("status_code") != 0
            or not uid.isdigit()
            or not isinstance(nickname, str)
            or not nickname
        ):
            raise PublishProblem("login_required")
        await page.locator('input[type="file"]').first.wait_for(state="attached", timeout=20000)
        await self.require_no_verification(page)
        return {"uid": uid, "nickname": nickname[:160]}

    async def require_no_verification(self, page):
        if urlsplit(page.url).path in {"", "/", "/login"}:
            raise PublishProblem("login_required")
        for text in ("手机号登录", "扫码登录", "安全验证", "请输入验证码"):
            for element in await page.get_by_text(text, exact=True).all():
                if await element.is_visible():
                    raise PublishProblem("verification_required")
        for element in await page.locator('input[placeholder*="验证码"]').all():
            if await element.is_visible():
                raise PublishProblem("verification_required")

    async def check(self, record):
        async with self.session(record) as (context, page):
            author = await self.open_upload(page)
            return dict(author, storage_state=await context.storage_state())

    async def search_on_page(self, page, keyword):
        data = await page.evaluate(
            """async (keyword) => {
            const url = new URL('/aweme/v1/search/challengesug/', location.origin);
            url.search = new URLSearchParams({keyword, source:'challenge_create', aid:'2906'});
            const response = await fetch(url, {credentials:'include'});
            if (!response.ok) throw new Error('topic search failed');
            return await response.json();
        }""",
            keyword,
        )
        if data.get("status_code", 0) != 0:
            raise PublishProblem("topic_search_failed")
        try:
            return candidates(data, "search")
        except ValueError:
            raise PublishProblem("topic_search_failed") from None

    async def search_topics(self, record, keyword):
        async with self.session(record) as (_context, page):
            await self.open_upload(page)
            return await self.search_on_page(page, keyword)

    async def upload_video(self, page, path):
        await page.locator('input[type="file"]').first.set_input_files(str(path))
        await page.wait_for_url(
            re.compile(r"https://creator\.douyin\.com/creator-micro/content/(publish|post/video)(?:\?|$)"),
            timeout=180000,
        )
        await page.locator('div.zone-container[contenteditable="true"]').first.wait_for(
            state="visible", timeout=180000
        )
        await page.locator('[class^="long-card"] div:has-text("重新上传")').first.wait_for(
            state="visible", timeout=600000
        )
        await self.require_no_verification(page)

    async def recommend(self, record, job, path):
        async with self.session(record) as (context, page):

            async def block_publication(route):
                if urlsplit(route.request.url).path.rstrip("/") in PUBLISH_PATHS:
                    return await route.abort()
                await route.fallback()

            await context.route("**/*", block_publication)
            author = await self.open_upload(page)
            if author["uid"] != job["account_uid"]:
                raise PublishProblem("account_changed")
            async with page.expect_response(
                lambda r: response_at(r, RECOMMEND_PATH), timeout=600000
            ) as pending:
                await self.upload_video(page, path)
            response = await pending.value
            if response.status != 200:
                raise PublishProblem("topic_recommend_failed")
            try:
                data = await response.json()
                if not isinstance(data, dict) or data.get("status_code", 0) != 0:
                    raise ValueError("Recommendation unavailable")
                recommendations = candidates(data, "recommend")
            except ValueError:
                raise PublishProblem("topic_recommend_failed") from None
            return dict(author, recommendations=recommendations, storage_state=await context.storage_state())

    async def fill_topics(self, page, editor, topics):
        for topic in topics:
            if topic.id is not None:
                results = await self.search_on_page(page, topic.name)
                if not any(t["id"] == topic.id and t["name"] == topic.name for t in results):
                    raise PublishProblem("topic_not_found")
            await editor.press("Control+End")
            await editor.press_sequentially(" #" + topic.name, delay=25)
            if topic.id is None:
                await editor.press("Space")
            else:
                row = (
                    page.locator('div[class*="tag-hash"]:not([class*="tag-hash-view"])')
                    .filter(has=page.get_by_text(topic.name, exact=True))
                    .first
                )
                await row.click(timeout=20000)
            await page.keyboard.press("Escape")
            await page.wait_for_function(
                """name => [...document.querySelectorAll('[data-mention="#"]')]
                .some(element => element.textContent.trim() === '#' + name)""",
                arg=topic.name,
                timeout=10000,
            )

    async def click_with_topic_guard(self, context, button, topics, on_submitting):
        observed = asyncio.Event()
        outcome = {"allowed": False, "blocked": False}

        async def guard(route):
            request = route.request
            url = urlsplit(request.url)
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                return await route.fallback()
            if (
                request.method != "POST"
                or url.scheme != "https"
                or url.hostname != "creator.douyin.com"
                or url.path.rstrip("/") not in PUBLISH_PATHS
            ):
                return await route.abort()
            if outcome["allowed"] or outcome["blocked"]:
                return await route.abort()
            try:
                verify_submission(request.post_data or "", topics)
            except ValueError:
                outcome["blocked"] = True
                observed.set()
                return await route.abort()
            outcome["allowed"] = True
            observed.set()
            await route.fallback()

        # After all uploads finish, fail closed for any unrecognized mutation.
        # Service workers are disabled so they cannot bypass this request boundary.
        await context.route("**/*", guard)
        on_submitting()
        await button.click(timeout=15000)
        try:
            await asyncio.wait_for(observed.wait(), timeout=20)
        except TimeoutError:
            raise PublishProblem("topics_unverified", submission_blocked=True) from None
        if outcome["blocked"]:
            raise PublishProblem("topics_unverified", submission_blocked=True)

    async def read_posts(self, page):
        async with page.expect_response(lambda r: response_at(r, WORK_PATH), timeout=30000) as pending:
            await page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
        response = await pending.value
        if response.status != 200:
            raise PublishProblem("result_unavailable")
        return list(posts_from(await response.json()))

    async def publish(self, record, job, path, on_submitting):
        async with self.session(record) as (context, page):
            author = await self.open_upload(page)
            if author["uid"] != job["account_uid"]:
                raise PublishProblem("account_changed")
            existing = {post["id"] for post in await self.read_posts(page)}
            author = await self.open_upload(page)
            if author["uid"] != job["account_uid"]:
                raise PublishProblem("account_changed")
            await self.upload_video(page, path)
            title = page.locator('input[placeholder*="填写作品标题"]').first
            await title.wait_for(state="visible", timeout=180000)
            await title.fill(job["title"])
            editor = page.locator('div.zone-container[contenteditable="true"]').first
            await editor.wait_for(state="visible", timeout=30000)
            await editor.fill(job["description"])
            topics = Topics(topics=job.get("topics", [])).topics
            await self.fill_topics(page, editor, topics)
            await page.keyboard.press("Escape")
            await page.locator('[class^="long-card"] div:has-text("重新上传")').first.wait_for(
                state="visible", timeout=600000
            )
            await self.require_no_verification(page)
            # Only dismiss product-tour overlays, never a platform verification dialog.
            close_tour = page.locator(".shepherd-cancel-icon")
            for button in await close_tour.all():
                if await button.is_visible():
                    await button.click()
            cover_notice = page.get_by_text("请设置封面后再发布", exact=True)
            if await cover_notice.count() and await cover_notice.first.is_visible():
                await page.locator('[class^="recommendCover-"]').first.click()
                confirm = page.get_by_text("是否确认应用此封面？", exact=True)
                if await confirm.count() and await confirm.first.is_visible():
                    await page.get_by_role("button", name="确定", exact=True).click()
            # No invented authorship declaration, third-party sync or scheduled publication.
            button = page.get_by_role("button", name="发布", exact=True)
            await button.wait_for(state="visible", timeout=15000)
            if not await button.is_enabled():
                raise PublishProblem("publish_not_ready")
            clicked_at = int(time.time())
            if topics:
                await self.click_with_topic_guard(context, button, topics, on_submitting)
            else:
                on_submitting()  # Persist the uncertainty boundary BEFORE the one final click.
                await button.click(timeout=15000)
            await page.wait_for_url(re.compile(re.escape(MANAGE_URL) + r"(?:\?|$)"), timeout=45000)
            fresh = await self.read_posts(page)
            matches = {
                post["id"]
                for post in fresh
                if post["id"] not in existing
                and isinstance(post["created"], (int, float))
                and post["created"] >= clicked_at - 30
                and (post["title"] == job["title"] or str(post["desc"]).startswith(job["title"]))
            }
            if len(matches) != 1:
                raise PublishProblem("result_unavailable")
            return {"post_id": matches.pop(), "storage_state": await context.storage_state()}
