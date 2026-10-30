import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .vault import Vault

log = logging.getLogger(__name__)
TERMINAL = {"complete", "cancelled", "expired", "error"}


class BusySession(Exception):
    pass


class LoginError(Exception):
    """A deliberate message suitable for a user; never an upstream exception."""


@dataclass
class LoginSession:
    label: str
    ttl: float
    replace_id: str | None = None
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    status: str = "starting"
    message: str = "正在准备登录二维码…"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    qr_image: bytes | None = None
    image_revision: int = 0
    account: dict | None = None
    task: asyncio.Task | None = None
    verification: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))

    def publish_qr(self, image: bytes) -> None:
        if self.status in TERMINAL:
            return
        if image != self.qr_image:
            self.qr_image = image
            self.image_revision += 1
        if self.status == "starting":
            self.status = "waiting"
            self.message = "请用手机抖音扫一扫，并在手机上确认登录。"

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "expires_at": (self.created_at + timedelta(seconds=self.ttl)).isoformat(),
            "qr_available": self.qr_image is not None,
            "image_revision": self.image_revision,
            "account": self.account,
            "replace_id": self.replace_id,
        }


class LoginManager:
    def __init__(self, vault: Vault, driver, ttl: float = 180):
        self.vault = vault
        self.driver = driver
        self.ttl = ttl
        self.sessions: dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, label: str, replace_id: str | None = None) -> LoginSession:
        async with self._lock:
            if any(session.status not in TERMINAL for session in self.sessions.values()):
                raise BusySession("已有扫码会话正在进行。")
            if replace_id:
                self.vault.export(replace_id)
            while len(self.sessions) >= 20:
                del self.sessions[next(iter(self.sessions))]
            session = LoginSession(label, self.ttl, replace_id)
            self.sessions[session.id] = session
            session.task = asyncio.create_task(self._run(session), name="douyin-qr-login")
            return session

    async def _run(self, session: LoginSession) -> None:
        try:
            async with asyncio.timeout(self.ttl):
                cookies, fingerprint, storage_state = await self.driver.run(session)
                session.account = self.vault.save(
                    cookies, fingerprint, session.label, session.replace_id, storage_state=storage_state
                )
                session.status = "complete"
                session.message = "登录信息已加密保存，可以导出或用于后续接入。"
        except TimeoutError:
            session.status = "expired"
            session.message = "本次扫码已超时，请重新获取二维码。"
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.message = "本次扫码已取消。"
        except LoginError as error:
            session.status = "error"
            session.message = str(error)
        except Exception as error:
            log.warning("login failed (%s)", type(error).__name__)
            session.status = "error"
            session.message = "暂时无法完成登录，请重新获取二维码；网络或平台验证可能影响本次会话。"
        finally:
            session.qr_image = None
            while not session.verification.empty():
                session.verification.get_nowait()

    def get(self, session_id: str) -> LoginSession:
        return self.sessions[session_id]

    async def cancel(self, session_id: str) -> LoginSession:
        session = self.get(session_id)
        if session.status not in TERMINAL and session.task:
            session.status = "cancelled"
            session.message = "本次扫码已取消。"
            session.qr_image = None
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.task
        return session

    async def close(self) -> None:
        for session in list(self.sessions.values()):
            await self.cancel(session.id)
