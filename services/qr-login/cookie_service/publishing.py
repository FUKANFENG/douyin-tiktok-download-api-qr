"""Durable, explicitly confirmed publishing. A submit is never automatically retried."""

import asyncio
import contextlib
import json
import re
import secrets
import shutil
import sqlite3
import time
from datetime import UTC, datetime

from pydantic import Field, field_validator

from .topics import Topics
from .vault import Vault

MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_STORAGE_BYTES = 10 * MAX_VIDEO_BYTES
ACTIVE = {"checking", "recommending", "uploading", "submitting"}
TERMINAL = {"published", "failed", "uncertain", "cancelled"}


class PublishProblem(Exception):
    def __init__(self, code: str, status: int = 409, *, submission_blocked=False):
        self.code = code
        self.status = status
        self.submission_blocked = submission_blocked
        super().__init__(code)


class NewJob(Topics):
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    client_key: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=30)
    description: str = Field(default="", max_length=1000)
    filename: str = Field(min_length=1, max_length=180)
    size: int = Field(ge=16, le=MAX_VIDEO_BYTES)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("title is blank")
        return value.strip()

    @field_validator("filename")
    @classmethod
    def video_filename(cls, value):
        if not re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f]+\.(mp4|mov|m4v)', value, re.I):
            raise ValueError("Expected an MP4, MOV or M4V basename")
        return value


def now():
    return datetime.now(UTC).isoformat()


class PublishManager:
    """One browser worker in the single-process credential service; SQLite records intent."""

    def __init__(self, vault: Vault, driver, account_busy=None):
        self.vault = vault
        self.driver = driver
        self.account_busy = account_busy or (lambda account_id: False)
        self.root = vault.root / "publishing"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.database = self.root / "jobs.sqlite3"
        self.tasks: dict[str, asyncio.Task] = {}
        self.searching_account = None
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, client_key TEXT UNIQUE NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL)"
            )
        # A process that vanished cannot prove the platform did not accept its final click.
        for job in self.list():
            if job["state"] in ACTIVE | {"receiving"}:
                self.update(
                    job["id"],
                    state="uncertain" if job["state"] == "submitting" else "failed",
                    error_code="interrupted",
                )
        for part in self.root.glob("*.part"):
            part.unlink(missing_ok=True)
        self.cleanup()

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def video_path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError(job_id)
        return self.root / f"{job_id}.mp4"

    def get(self, job_id):
        self.video_path(job_id)
        with self.connect() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return self.decode(row[0])

    @staticmethod
    def decode(value):
        job = json.loads(value)
        job.setdefault("topics", [])
        job.setdefault("topics_revision", "0" * 32)
        job.setdefault("recommended_topics", [])
        job.setdefault("recommendations_loaded", False)
        return job

    def list(self, limit=None):
        with self.connect() as db:
            return [
                self.decode(row[0])
                for row in db.execute(
                    "SELECT data FROM jobs ORDER BY rowid DESC LIMIT ?", (-1 if limit is None else limit,)
                )
            ]

    def update(self, job_id, *, expected=None, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = self.decode(row[0])
            if expected is not None and job["state"] not in expected:
                raise PublishProblem("state_changed")
            job.update(changes, updated_at=now())
            db.execute("UPDATE jobs SET state=?, data=? WHERE id=?", (job["state"], json.dumps(job), job_id))
        return job

    def cleanup(self):
        for job in self.list():
            age = time.time() - datetime.fromisoformat(job["created_at"]).timestamp()
            if age > 86400 and job["state"] not in ACTIVE | {"receiving"}:
                self.video_path(job["id"]).unlink(missing_ok=True)
                if job["state"] not in TERMINAL:
                    self.update(job["id"], state="cancelled", error_code="draft_expired")

    def create(self, data):
        body = NewJob.model_validate(data)
        self.cleanup()
        record = self.vault.export(body.account_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT data FROM jobs WHERE client_key=?", (body.client_key,)).fetchone()
            if previous:
                job = self.decode(previous[0])
                if any(job[key] != value for key, value in body.model_dump().items()):
                    raise PublishProblem("idempotency_conflict")
                return job
            reserved = sum(
                j["size"]
                for j in self.list()
                if self.video_path(j["id"]).exists() or j["state"] in {"awaiting_upload", "receiving"}
            )
            if (
                reserved + body.size > MAX_STORAGE_BYTES
                or shutil.disk_usage(self.root).free < body.size + 128 * 1024 * 1024
            ):
                raise PublishProblem("storage_full", 413)
            job = dict(
                body.model_dump(),
                id=secrets.token_hex(16),
                state="awaiting_upload",
                account_label=record["label"],
                account_name="",
                account_uid="",
                account_revision="",
                topics_revision=secrets.token_hex(16),
                recommended_topics=[],
                recommendations_loaded=False,
                error_code=None,
                post_id=None,
                created_at=now(),
                updated_at=now(),
            )
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?)",
                (job["id"], body.client_key, job["state"], json.dumps(job)),
            )
        return job

    async def receive(self, job_id, stream):
        job = self.update(job_id, expected={"awaiting_upload"}, state="receiving")
        target = self.video_path(job_id)
        part = target.with_suffix(".part")
        size = 0
        header = b""
        try:
            async with asyncio.timeout(900):
                with part.open("xb") as output:
                    async for chunk in stream:
                        size += len(chunk)
                        if size > job["size"] or size > MAX_VIDEO_BYTES:
                            raise PublishProblem("video_too_large", 413)
                        header = (header + chunk)[:16] if len(header) < 16 else header
                        output.write(chunk)
            if size != job["size"] or header[4:8] != b"ftyp":
                raise PublishProblem("invalid_video", 400)
            part.replace(target)
            return self.update(job_id, state="uploaded")
        except BaseException:
            part.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            self.update(job_id, state="failed", error_code="upload_incomplete")
            raise

    def busy(self, account_id=None):
        return (self.searching_account is not None and account_id in {None, self.searching_account}) or any(
            not task.done() and (account_id is None or self.get(key)["account_id"] == account_id)
            for key, task in self.tasks.items()
        )

    def ensure_available(self, account_id):
        if self.busy() or self.account_busy(account_id):
            raise PublishProblem("account_busy")

    def prepare(self, job_id):
        job = self.get(job_id)
        self.ensure_available(job["account_id"])
        if job["state"] not in {"uploaded", "ready", "failed"} or not self.video_path(job_id).is_file():
            raise PublishProblem("not_ready")
        record = self.vault.export(job["account_id"])
        changed = record["updated_at"] != job["account_revision"]
        self.update(
            job_id,
            state="checking",
            error_code=None,
            **({"recommended_topics": [], "recommendations_loaded": False} if changed else {}),
        )
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, submit=False))
        return self.get(job_id)

    def set_topics(self, job_id, topics, revision):
        chosen = Topics(topics=topics).model_dump()["topics"]
        job = self.get(job_id)
        if job["state"] not in {"awaiting_upload", "uploaded", "ready", "failed"}:
            raise PublishProblem("not_ready")
        if job["topics_revision"] != revision:
            raise PublishProblem("topics_changed")
        return self.update(job_id, topics=chosen, topics_revision=secrets.token_hex(16))

    async def search_topics(self, account_id, keyword):
        self.ensure_available(account_id)
        record = self.vault.export(account_id)
        if record.get("expires_at") and datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC):
            raise PublishProblem("login_expired")
        self.searching_account = account_id
        try:
            record["storage_state"] = self.vault.browser_state(account_id)
            async with asyncio.timeout(90):
                return await self.driver.search_topics(record, keyword)
        except PublishProblem:
            raise
        except Exception:
            raise PublishProblem("topic_search_failed", 503) from None
        finally:
            self.searching_account = None

    def recommend(self, job_id):
        job = self.get(job_id)
        self.ensure_available(job["account_id"])
        if job["state"] != "ready" or not self.video_path(job_id).is_file():
            raise PublishProblem("not_ready")
        self.update(
            job_id, state="recommending", error_code=None, recommended_topics=[], recommendations_loaded=False
        )
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, submit=False, recommend=True))
        return self.get(job_id)

    def submit(self, job_id, account_id, topics_revision=None):
        job = self.get(job_id)
        if job["account_id"] != account_id:
            raise PublishProblem("account_changed")
        # Repeated HTTP submissions observe the original intent, including uncertain outcomes.
        if job["state"] in {"uploading", "submitting", "published", "uncertain", "cancelled"}:
            return job
        if job["state"] != "ready":
            raise PublishProblem("not_ready")
        if (job["topics"] or topics_revision is not None) and topics_revision != job["topics_revision"]:
            raise PublishProblem("topics_changed")
        self.ensure_available(account_id)
        record = self.vault.export(account_id)
        if record["updated_at"] != job["account_revision"]:
            raise PublishProblem("account_changed")
        if time.time() - datetime.fromisoformat(job["checked_at"]).timestamp() > 900:
            raise PublishProblem("check_expired")
        self.update(job_id, expected={"ready"}, state="uploading", error_code=None)
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, submit=True))
        return self.get(job_id)

    async def _run(self, job_id, *, submit, recommend=False):
        job = self.get(job_id)
        try:
            record = self.vault.export(job["account_id"])
            if record.get("expires_at") and datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC):
                raise PublishProblem("login_expired")
            if (submit or recommend) and record["updated_at"] != job["account_revision"]:
                raise PublishProblem("account_changed")
            record["storage_state"] = self.vault.browser_state(job["account_id"])
            async with asyncio.timeout(900 if submit or recommend else 90):
                if submit:
                    result = await self.driver.publish(
                        record, job, self.video_path(job_id), lambda: self.update(job_id, state="submitting")
                    )
                elif recommend:
                    result = await self.driver.recommend(record, job, self.video_path(job_id))
                else:
                    result = await self.driver.check(record)
            saved = self.vault.update_browser_state(
                job["account_id"], result["storage_state"], expected_revision=record["updated_at"]
            )
            if submit:
                self.update(
                    job_id,
                    state="published",
                    post_id=result.get("post_id"),
                    account_revision=saved["updated_at"],
                )
            else:
                if not result.get("uid") or not result.get("nickname"):
                    raise PublishProblem("account_unverified")
                self.update(
                    job_id,
                    state="ready",
                    account_revision=saved["updated_at"],
                    account_name=result["nickname"],
                    account_uid=result["uid"],
                    checked_at=now(),
                    **(
                        {"recommended_topics": result["recommendations"], "recommendations_loaded": True}
                        if recommend
                        else {}
                    ),
                )
        except BaseException as error:
            current = self.get(job_id)
            uncertain = current["state"] == "submitting" and not (
                isinstance(error, PublishProblem) and error.submission_blocked
            )
            self.update(
                job_id,
                state="uncertain" if uncertain else "failed",
                error_code="submission_unknown"
                if uncertain
                else error.code
                if isinstance(error, PublishProblem)
                else "interrupted"
                if isinstance(error, asyncio.CancelledError)
                else "platform_unavailable",
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if self.get(job_id)["state"] in {"published", "uncertain"}:
                self.video_path(job_id).unlink(missing_ok=True)
            self.tasks.pop(job_id, None)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job["state"] in ACTIVE | {"receiving"}:
            raise PublishProblem("account_busy")
        if job["state"] not in {"published", "uncertain"}:
            job = self.update(job_id, state="cancelled")
        self.video_path(job_id).unlink(missing_ok=True)
        return job

    async def wait(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()))

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.wait()
