"""Local owner authentication for the SneakChat admin API."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

COOKIE_NAME = "sneakchat_admin"
SESSION_SECONDS = 12 * 60 * 60
PBKDF2_ROUNDS = 310_000


def auth_file() -> Path:
    override = os.environ.get("SNEAKCHAT_ADMIN_AUTH_FILE")
    if override:
        return Path(override)
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / "SneakChat" / "admin_auth.json"


def read_auth() -> dict | None:
    try:
        return json.loads(auth_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def make_auth_record(password: str) -> dict:
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return {
        "version": 1,
        "rounds": PBKDF2_ROUNDS,
        "salt": salt.hex(),
        "password_hash": digest.hex(),
        "signing_key": secrets.token_hex(32),
    }


def verify_password(password: str, config: dict) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(config["salt"]), int(config["rounds"])
        ).hex()
        return hmac.compare_digest(digest, str(config["password_hash"]))
    except (KeyError, ValueError, TypeError):
        return False


def issue_session(config: dict) -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(bytes.fromhex(config["signing_key"]), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_session(token: str | None, config: dict) -> bool:
    try:
        issued, nonce, signature = token.split(".", 2)
        payload = f"{issued}.{nonce}"
        expected = hmac.new(bytes.fromhex(config["signing_key"]), payload.encode(), hashlib.sha256).hexdigest()
        age = int(time.time()) - int(issued)
        return 0 <= age <= SESSION_SECONDS and hmac.compare_digest(signature, expected)
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


def require_local_request(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    host = request.url.hostname or ""
    try:
        local = ipaddress.ip_address(client_host).is_loopback and (
            host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        )
    except ValueError:
        local = False
    if not local:
        raise HTTPException(status_code=403, detail="Admin доступен только на этом компьютере")

    origin = request.headers.get("origin")
    if origin:
        origin_host = urlsplit(origin).hostname or ""
        try:
            same_local = origin_host.lower() == "localhost" or ipaddress.ip_address(origin_host).is_loopback
        except ValueError:
            same_local = False
        if not same_local or origin_host.lower() != host.lower():
            raise HTTPException(status_code=403, detail="Недопустимый источник запроса")


def require_admin(request: Request) -> dict:
    require_local_request(request)
    config = read_auth()
    if config is None:
        raise HTTPException(status_code=503, detail="Сначала настройте пароль владельца")
    if not valid_session(request.cookies.get(COOKIE_NAME), config):
        raise HTTPException(status_code=401, detail="Требуется вход владельца")
    return config
