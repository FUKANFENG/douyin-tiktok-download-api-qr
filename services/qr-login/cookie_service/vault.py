"""Encrypted credential storage. Public summaries never contain cookie values."""

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from cryptography.fernet import Fernet, InvalidToken


def cookie_values(cookies: list[dict]) -> dict[str, str]:
    return {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}


def usable_login(cookies: list[dict]) -> bool:
    values = cookie_values(cookies)
    return bool(
        values.get("ttwid")
        and (values.get("sessionid") or values.get("sessionid_ss"))
        and values.get("sid_tt")
    )


def login_expiry(cookies: list[dict]) -> datetime | None:
    """sid_guard declares issue time + lifetime; guest token expiry is unrelated."""
    values = cookie_values(cookies)
    candidates = []
    try:
        parts = unquote(values.get("sid_guard", "")).split("|")
        if len(parts) >= 3 and int(parts[1]) > 0 and int(parts[2]) > 0:
            candidates.append(int(parts[1]) + int(parts[2]))
    except (ValueError, OverflowError):
        pass
    for cookie in cookies:
        if cookie.get("name") in {"sessionid", "sessionid_ss", "sid_tt"}:
            try:
                expiry = float(cookie.get("expires", -1))
                if expiry > 0:
                    candidates.append(expiry)
            except (ValueError, TypeError):
                continue
    try:
        return datetime.fromtimestamp(min(candidates), UTC) if candidates else None
    except (ValueError, OverflowError, OSError):
        return None


class Vault:
    def __init__(self, root: Path):
        self.root = root
        self.records = root / "accounts"
        self.records.mkdir(parents=True, exist_ok=True)
        key_path = root / "vault.key"
        if not key_path.exists():
            if any(self.records.glob("*.enc")):
                raise RuntimeError("保存的登录信息缺少原始密钥，请恢复 vault.key。")
            with key_path.open("xb") as file:
                file.write(Fernet.generate_key())
            os.chmod(key_path, 0o600)
        key = key_path.read_bytes()
        self.cipher = Fernet(key)
        configured_token = os.environ.get("DY_COOKIE_API_TOKEN", "")
        if configured_token and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", configured_token):
            raise RuntimeError("DY_COOKIE_API_TOKEN must contain 32-256 URL-safe characters.")
        self.api_token = configured_token or hmac.new(key, b"local-api", hashlib.sha256).hexdigest()
        self.csrf_token = hmac.new(key, b"browser-csrf", hashlib.sha256).hexdigest()

    def _path(self, account_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", account_id):
            raise KeyError(account_id)
        path = self.records / f"{account_id}.enc"
        if not path.is_file():
            raise KeyError(account_id)
        return path

    def export(self, account_id: str) -> dict:
        try:
            return json.loads(self.cipher.decrypt(self._path(account_id).read_bytes()))
        except (InvalidToken, json.JSONDecodeError) as error:
            raise RuntimeError("登录信息无法解密，请检查原始密钥。") from error

    @staticmethod
    def summary(record: dict) -> dict:
        expiry = datetime.fromisoformat(record["expires_at"]) if record.get("expires_at") else None
        remaining = (expiry - datetime.now(UTC)).total_seconds() if expiry else None
        return {
            "id": record["id"],
            "label": record["label"],
            "platform": "douyin",
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "expires_at": record["expires_at"],
            "cookie_count": len(record["cookies"]),
            "browser_version": record["fingerprint"].get("browser_version", ""),
            "expiry_state": "unknown"
            if remaining is None
            else "expired"
            if remaining <= 0
            else "expiring"
            if remaining <= 3 * 86400
            else "valid",
        }

    def list_accounts(self) -> list[dict]:
        accounts = [self.summary(self.export(path.stem)) for path in self.records.glob("*.enc")]
        return sorted(accounts, key=lambda account: account["updated_at"], reverse=True)

    def browser_state(self, account_id: str) -> dict:
        record = self.export(account_id)
        return record.get("storage_state") or {"cookies": record["cookies"], "origins": []}

    def update_browser_state(self, account_id: str, state: dict, *, expected_revision: str) -> dict:
        record = self.export(account_id)
        if record["updated_at"] != expected_revision:
            raise ValueError("Account changed; check the renewed login before publishing.")
        # A creator-only cookie is not a cookie for the collection site's HTTP requests.
        cookies = [
            c
            for c in state["cookies"]
            if c.get("domain", "").lstrip(".") in {"douyin.com", "www.douyin.com"}
            and c.get("path", "/") == "/"
        ]
        return self.save(cookies, record["fingerprint"], record["label"], account_id, storage_state=state)

    def save(
        self,
        cookies: list[dict],
        fingerprint: dict,
        label: str,
        replace_id: str | None = None,
        *,
        storage_state: dict | None = None,
    ) -> dict:
        if not usable_login(cookies) or not fingerprint.get("user_agent"):
            raise ValueError("尚未收集到完整的抖音登录 Cookie 和浏览器信息。")
        previous = self.export(replace_id) if replace_id else None
        account_id = previous["id"] if previous else secrets.token_hex(16)
        now = datetime.now(UTC).isoformat()
        expiry = login_expiry(cookies)
        record = {
            "version": 1,
            "id": account_id,
            "platform": "douyin",
            "label": label.strip() or (previous["label"] if previous else "我的抖音"),
            "created_at": previous["created_at"] if previous else now,
            "updated_at": now,
            "expires_at": expiry.isoformat() if expiry else None,
            "cookies": cookies,
            "cookie_header": "; ".join(f"{name}={value}" for name, value in cookie_values(cookies).items()),
            "user_agent": fingerprint["user_agent"],
            "fingerprint": fingerprint,
        }
        if storage_state is not None:
            record["storage_state"] = storage_state
        encrypted = self.cipher.encrypt(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.records, delete=False) as file:
                temporary = Path(file.name)
                file.write(encrypted)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.records / f"{account_id}.enc")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return self.summary(record)

    def delete(self, account_id: str) -> None:
        self._path(account_id).unlink()
