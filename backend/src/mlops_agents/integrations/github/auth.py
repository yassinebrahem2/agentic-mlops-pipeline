from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Literal, Protocol

import httpx2
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import now_iso
from mlops_agents.credentials import KeyStore, KeyStoreError
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import get_login
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import integrations

GITHUB = "github"
GITHUB_KEYSTORE_REF = "github/token"


class GitHubStatus(BaseModel):
    mode: Literal["none", "gh", "token"]
    login: str | None
    gh_available: bool


class GhLike(Protocol):
    @property
    def available(self) -> bool: ...

    async def token(self) -> str: ...


class GhCli:
    """The GitHub CLI. Its token is read on every use, never copied, so a new `gh auth login`
    is picked up automatically."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path if path is not None else shutil.which("gh")

    @property
    def available(self) -> bool:
        return self._path is not None

    async def token(self) -> str:
        if self._path is None:
            raise DomainError(
                "gh_not_installed",
                "The GitHub CLI (gh) is not installed. Install it, or paste a token instead.",
                status=409,
            )
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [self._path, "auth", "token", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise DomainError(
                "gh_timeout", "The GitHub CLI did not answer within 30 seconds.", status=504
            ) from None
        token = completed.stdout.strip()
        if completed.returncode != 0 or not token:
            raise DomainError(
                "gh_not_logged_in",
                "The GitHub CLI is not logged in. Run `gh auth login`, then try again.",
                status=409,
            )
        return token


class GitHubAuth:
    def __init__(
        self, databases: Databases, keystore: KeyStore, http: httpx2.AsyncClient, gh: GhLike
    ) -> None:
        self._dbs = databases
        self._keys = keystore
        self._http = http
        self._gh = gh

    async def _mode(self) -> tuple[str, str | None]:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(integrations).where(integrations.c.name == GITHUB)))
                .mappings()
                .first()
            )
        return (row["mode"], row["login"]) if row else ("none", None)

    async def _save(self, mode: str, login: str) -> None:
        engine = await self._dbs.app()
        values = {"name": GITHUB, "mode": mode, "login": login, "verified_at": now_iso()}
        statement = sqlite_insert(integrations).values(values)
        async with engine.begin() as conn:
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=["name"], set_={k: v for k, v in values.items() if k != "name"}
                )
            )

    async def status(self) -> GitHubStatus:
        mode, login = await self._mode()
        # model_validate checks the stored mode against GitHubStatus's Literal.
        return GitHubStatus.model_validate(
            {"mode": mode, "login": login, "gh_available": self._gh.available}
        )

    async def enable_gh(self) -> GitHubStatus:
        login = await get_login(self._http, await self._gh.token())
        await self._save("gh", login)
        await asyncio.to_thread(self._keys.delete, GITHUB_KEYSTORE_REF)
        return await self.status()

    async def set_token(self, value: str) -> GitHubStatus:
        value = value.strip()
        if not value:
            raise DomainError("token_empty", "Paste a token first.", status=422)
        login = await get_login(self._http, value)
        await asyncio.to_thread(self._keys.set, GITHUB_KEYSTORE_REF, value)
        await self._save("token", login)
        return await self.status()

    async def disconnect(self) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(integrations).where(integrations.c.name == GITHUB))
        await asyncio.to_thread(self._keys.delete, GITHUB_KEYSTORE_REF)

    async def token(self) -> str | None:
        mode, _ = await self._mode()
        if mode == "gh":
            return await self._gh.token()
        if mode == "token":
            try:
                return await asyncio.to_thread(self._keys.get, GITHUB_KEYSTORE_REF)
            except KeyStoreError as err:  # its messages never contain a key
                raise DomainError(
                    "github_token_unreadable",
                    f"The saved GitHub token could not be read: {err}",
                    status=503,
                ) from None
        return None

    async def require_token(self) -> str:
        token = await self.token()
        if token is None:
            raise DomainError(
                "github_not_connected", "Connect GitHub in Integrations first.", status=409
            )
        return token
