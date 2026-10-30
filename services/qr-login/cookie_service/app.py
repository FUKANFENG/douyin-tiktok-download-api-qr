import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .browser import BrowserLogin, find_browser
from .publisher import DouyinPublisher
from .publishing import PublishManager, PublishProblem
from .publishing_api import router as publishing_router
from .sessions import TERMINAL, BusySession, LoginManager
from .vault import Vault

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = PROJECT_ROOT.parent.parent / "work" / "douyin-cookie-service-data"
COOKIE_NAME = "dy_cookie_service"
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
}


class StartLogin(BaseModel):
    label: str = Field(default="", max_length=80)
    replace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class VerificationCode(BaseModel):
    code: str = Field(pattern=r"^[0-9]{4,8}$")


def create_app(data_dir: Path | None = None, driver=None, ttl: float = 180, publisher=None) -> FastAPI:
    web_ui = os.environ.get("DY_COOKIE_WEB_UI", "true").lower() == "true"
    vault = Vault(Path(data_dir or os.environ.get("DY_COOKIE_DATA_DIR", DEFAULT_DATA)))
    manager = LoginManager(vault, driver or BrowserLogin(), ttl=ttl)
    publishing = PublishManager(
        vault,
        publisher or DouyinPublisher(),
        account_busy=lambda _account_id: any(s.status not in TERMINAL for s in manager.sessions.values()),
    )

    # FastAPI's lifespan owns the live browser tasks, including shutdown cleanup.
    # https://fastapi.tiangolo.com/advanced/events/
    @asynccontextmanager
    async def lifespan(app):
        yield
        await manager.close()
        await publishing.close()

    app = FastAPI(
        title="抖音扫码登录服务", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.vault = vault
    app.state.manager = manager
    app.state.publishing = publishing
    app.include_router(publishing_router)

    @app.exception_handler(PublishProblem)
    async def publishing_problem(request, error):
        return JSONResponse({"detail": error.code}, status_code=error.status, headers=SECURITY_HEADERS)

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        from urllib.parse import urlsplit

        def reject(message: str, status: int):
            return JSONResponse({"detail": message}, status_code=status, headers=SECURITY_HEADERS)

        try:
            hostname = urlsplit(f"http://{request.headers.get('host', '')}").hostname
        except ValueError:
            return reject("无效的本机访问地址。", 400)
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            return reject("服务仅支持本机访问。", 400)
        protected = request.url.path.startswith("/api/") or request.url.path == "/openapi.json"
        if protected:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return reject("拒绝来自其它网站的请求。", 403)
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers['host']}":
                return reject("请求来源不匹配。", 403)
            authorization = request.headers.get("authorization", "")
            bearer = authorization.startswith("Bearer ")
            candidate = (
                authorization[7:]
                if bearer
                else ""
                if authorization or not web_ui
                else request.cookies.get(COOKIE_NAME, "")
            )
            if not hmac.compare_digest(candidate.encode(), vault.api_token.encode()):
                return reject("请先打开本地登录页面。", 401)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not bearer:
                if not hmac.compare_digest(
                    request.headers.get("x-dy-csrf", "").encode(), vault.csrf_token.encode()
                ):
                    return reject("请刷新页面后重试。", 403)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(KeyError)
    async def missing_record(request, error):
        return JSONResponse({"detail": "没有找到这份登录信息或扫码会话。"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": "输入格式不正确，请检查填写内容。"}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def storage_error(request, error):
        return JSONResponse({"detail": "无法读取保存的登录信息，请检查原始数据目录和密钥。"}, status_code=503)

    @app.get("/", include_in_schema=False)
    async def index():
        if not web_ui:
            raise HTTPException(404, "Standalone UI is disabled; use the download console.")
        response = FileResponse(PROJECT_ROOT / "static" / "index.html")
        response.set_cookie(COOKIE_NAME, vault.api_token, httponly=True, samesite="strict", max_age=8 * 3600)
        return response

    @app.get("/healthz", include_in_schema=False)
    async def health():
        return {
            "service": "douyin-cookie-service",
            "status": "ok",
            "version": __version__,
            "browser_available": bool(find_browser()),
        }

    @app.get("/api/bootstrap")
    async def bootstrap():
        latest = next(reversed(manager.sessions.values()), None)
        return {
            "csrf_token": vault.csrf_token,
            "accounts": vault.list_accounts(),
            "session": latest.public() if latest else None,
        }

    @app.post("/api/logins", status_code=202)
    async def start_login(body: StartLogin):
        if publishing.busy():
            raise HTTPException(409, "正在检查或发布视频，请稍后扫码。")
        try:
            return (await manager.start(body.label, body.replace_id)).public()
        except BusySession as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/logins/{session_id}")
    async def login_status(session_id: str):
        return manager.get(session_id).public()

    @app.get("/api/logins/{session_id}/qr")
    async def login_qr(session_id: str):
        image = manager.get(session_id).qr_image
        if image is None:
            raise HTTPException(404, "二维码尚未生成或本次扫码已经结束。")
        return Response(image, media_type="image/png")

    @app.delete("/api/logins/{session_id}")
    async def cancel_login(session_id: str):
        return (await manager.cancel(session_id)).public()

    @app.post("/api/logins/{session_id}/code", status_code=202)
    async def verification_code(session_id: str, body: VerificationCode):
        session = manager.get(session_id)
        if session.status != "verification_required" or session.verification.full():
            raise HTTPException(409, "当前没有等待输入验证码，或上一份验证码仍在提交中。")
        session.verification.put_nowait(body.code)
        return {"submitted": True}

    @app.get("/api/accounts")
    async def accounts():
        return vault.list_accounts()

    @app.get("/api/accounts/{account_id}/export")
    async def export_account(account_id: str):
        return JSONResponse(
            vault.export(account_id),
            headers={"Content-Disposition": f'attachment; filename="douyin-login-{account_id}.json"'},
        )

    @app.get("/api/accounts/{account_id}/cookie")
    async def copy_cookie(account_id: str):
        return {"cookie_header": vault.export(account_id)["cookie_header"]}

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str):
        if publishing.busy(account_id):
            raise HTTPException(409, "正在使用此账号发布视频，请稍后删除。")
        if any(
            session.replace_id == account_id and session.status not in TERMINAL
            for session in manager.sessions.values()
        ):
            raise HTTPException(409, "正在为该账号重新扫码，请先取消扫码再删除。")
        vault.delete(account_id)
        return {"deleted": True}

    @app.post("/api/shutdown", include_in_schema=False)
    async def shutdown():
        callback = getattr(app.state, "shutdown_callback", None)
        if callback is None:
            raise HTTPException(503, "当前运行方式不支持远程停止。")
        asyncio.get_running_loop().call_later(0.2, callback)
        return {"stopping": True}

    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")
    return app
