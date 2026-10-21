"""Sign-in: the access token in local mode, the password in server mode, and sign-out."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from time import monotonic
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from mlops_agents.api.deps import ServicesDep
from mlops_agents.api.hosts import HostRules
from mlops_agents.api.security import CSRF_COOKIE, SESSION_COOKIE, SESSION_DAYS, SessionSigner
from mlops_agents.config import RunMode, Settings
from mlops_agents.errors import DomainError

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_WINDOW_SECONDS = 60.0
MAX_FAILED_LOGINS = 5


class AuthStatus(BaseModel):
    mode: Literal["local", "server"]
    signed_in: bool


class TokenSignIn(BaseModel):
    token: str = Field(repr=False)


class PasswordSignIn(BaseModel):
    password: str = Field(repr=False)


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


class PasswordHash:
    """The server password's scrypt digest, kept in memory only; the password itself is not."""

    __slots__ = ("_digest", "_salt")

    def __init__(self, salt: bytes, digest: bytes) -> None:
        self._salt = salt
        self._digest = digest

    @classmethod
    async def of(cls, password: str) -> PasswordHash:
        salt = secrets.token_bytes(16)
        # scrypt is slow on purpose: never on the event loop.
        return cls(salt, await asyncio.to_thread(_scrypt, password, salt))

    async def matches(self, attempt: str) -> bool:
        digest = await asyncio.to_thread(_scrypt, attempt, self._salt)
        return hmac.compare_digest(digest, self._digest)


class LoginLimiter:
    """At most MAX_FAILED_LOGINS failed logins per client address per window, in memory."""

    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = {}

    def reserve(self, address: str) -> float | None:
        """Counts an attempt as failed until `forgive` says otherwise, so attempts running at
        the same time cannot pass the limit. None when the address is over the limit."""
        now = monotonic()
        self._attempts = {
            key: recent
            for key, times in self._attempts.items()
            if (recent := [at for at in times if now - at < LOGIN_WINDOW_SECONDS])
        }
        attempts = self._attempts.setdefault(address, [])
        if len(attempts) >= MAX_FAILED_LOGINS:
            return None
        attempts.append(now)
        return now

    def forgive(self, address: str, at: float) -> None:
        attempts = self._attempts.get(address, [])
        if at in attempts:
            attempts.remove(at)


def _status(settings: Settings, signed_in: bool) -> AuthStatus:
    mode: Literal["local", "server"] = "server" if settings.mode is RunMode.SERVER else "local"
    return AuthStatus(mode=mode, signed_in=signed_in)


# Each cookie's name and whether scripts are kept from reading it (the UI reads the CSRF token).
_COOKIES = ((SESSION_COOKIE, True), (CSRF_COOKIE, False))


def _sign_in(request: Request, response: Response, settings: Settings) -> AuthStatus:
    signer: SessionSigner = request.app.state.signer
    session, csrf = signer.issue()
    values = {SESSION_COOKIE: session, CSRF_COOKIE: csrf}
    for name, http_only in _COOKIES:
        response.set_cookie(
            name,
            values[name],
            max_age=SESSION_DAYS * 86400,
            path="/",
            secure=settings.mode is RunMode.SERVER,
            httponly=http_only,
            samesite="strict",
        )
    return _status(settings, signed_in=True)


@router.get("/status")
async def auth_status(request: Request, services: ServicesDep) -> AuthStatus:
    signer: SessionSigner = request.app.state.signer
    nonce = signer.check(request.cookies.get(SESSION_COOKIE))
    return _status(services.settings, signed_in=nonce is not None)


@router.post("/token")
async def sign_in_with_token(
    body: TokenSignIn, request: Request, response: Response, services: ServicesDep
) -> AuthStatus:
    settings = services.settings
    if settings.mode is not RunMode.LOCAL:
        raise DomainError(
            "not_available",
            "This app signs in with a password. Open the sign-in page and enter it.",
            status=404,
        )
    access_token: str = request.app.state.access_token
    if not hmac.compare_digest(body.token.encode(), access_token.encode()):
        raise DomainError(
            "bad_token",
            "This link is out of date. Open the address printed in the terminal where the app runs.",
            status=401,
        )
    return _sign_in(request, response, settings)


@router.post("/login")
async def sign_in_with_password(
    body: PasswordSignIn, request: Request, response: Response, services: ServicesDep
) -> AuthStatus:
    settings = services.settings
    if settings.mode is not RunMode.SERVER:
        raise DomainError(
            "not_available",
            "This app signs in with the address printed in the terminal. Open that address.",
            status=404,
        )
    rules: HostRules = request.app.state.host_rules
    limiter: LoginLimiter = request.app.state.login_limiter
    password_hash: PasswordHash = request.app.state.password_hash
    address = rules.client_address(request.scope, request.headers)
    attempt = limiter.reserve(address)
    if attempt is None:
        raise DomainError(
            "too_many_attempts", "Too many attempts. Wait a minute and try again.", status=429
        )
    if not await password_hash.matches(body.password):
        raise DomainError("bad_password", "Wrong password.", status=401)
    limiter.forgive(address, attempt)
    return _sign_in(request, response, settings)


@router.post("/logout")
async def sign_out(response: Response, services: ServicesDep) -> AuthStatus:
    """Ends the session in this browser only: sessions are stateless signed cookies."""
    settings = services.settings
    for name, http_only in _COOKIES:
        response.delete_cookie(
            name,
            path="/",
            secure=settings.mode is RunMode.SERVER,
            httponly=http_only,
            samesite="strict",
        )
    return _status(settings, signed_in=False)
