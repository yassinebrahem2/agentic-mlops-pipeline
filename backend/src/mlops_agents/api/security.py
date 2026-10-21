"""Signed session cookies and the guard that requires them on /api."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from mlops_agents.api.errors import error_response
from mlops_agents.paths import DataPaths

SESSION_DAYS = 30
SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

# The sign-in routes: there is no session yet (the Origin check and SameSite=Strict protect them).
_OPEN = frozenset(
    {("GET", "/api/auth/status"), ("POST", "/api/auth/token"), ("POST", "/api/auth/login")}
)
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def load_secret(paths: DataPaths) -> bytes:
    """32 random bytes kept in the data folder, so sign-ins survive restarts."""
    path = paths.cookie_secret
    if path.exists():
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    secret = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(secret.hex(), encoding="ascii")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)
    return secret


class SessionSigner:
    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def _mac(self, text: str) -> str:
        digest = hmac.new(self._secret, text.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def issue(self) -> tuple[str, str]:
        nonce = secrets.token_urlsafe(16)
        body = f"{int(time.time())}.{nonce}"
        return f"{body}.{self._mac('session:' + body)}", self.csrf_for(nonce)

    def check(self, cookie: str | None) -> str | None:
        if not cookie or cookie.count(".") != 2:
            return None
        issued, nonce, mac = cookie.split(".")
        if not hmac.compare_digest(mac.encode(), self._mac(f"session:{issued}.{nonce}").encode()):
            return None
        if not issued.isdigit() or time.time() - int(issued) > SESSION_DAYS * 86400:
            return None
        return nonce

    def csrf_for(self, nonce: str) -> str:
        return self._mac("csrf:" + nonce)


class SessionGuard:
    """Refuses /api requests without a valid session, and state-changing ones without the
    CSRF header. Pure ASGI, so streamed responses pass through unbuffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _guarded(scope["path"]):
            refusal = _refusal(scope)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _guarded(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _refusal(scope: Scope) -> ASGIApp | None:
    method: str = scope["method"]
    if (method, scope["path"]) in _OPEN:
        return None
    signer: SessionSigner = scope["app"].state.signer
    connection = HTTPConnection(scope)
    nonce = signer.check(connection.cookies.get(SESSION_COOKIE))
    if nonce is None:
        return error_response(401, "not_signed_in", "Sign in again.")
    if method in _STATE_CHANGING:
        sent = connection.headers.get(CSRF_HEADER, "")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII str.
        if not hmac.compare_digest(sent.encode(), signer.csrf_for(nonce).encode()):
            return error_response(403, "csrf_failed", "Reload the page and try again.")
    return None
