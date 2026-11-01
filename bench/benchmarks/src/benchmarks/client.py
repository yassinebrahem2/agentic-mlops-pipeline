"""HTTP client for interacting with the app during benchmarks."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx2
from fastapi import FastAPI


class BenchClient(Protocol):
    """Protocol for bench HTTP client."""

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Any: ...

    async def post(self, path: str, body: Mapping[str, Any]) -> Any: ...


class BenchHttpError(Exception):
    """Raised when an HTTP request fails.

    Holds only route and status, never the request or response body.
    """

    def __init__(self, route: str, status: int) -> None:
        super().__init__(f"HTTP {status} for {route}")
        self.route = route
        self.status = status
        self.status_code = status


def _normalize_path(path: str) -> str:
    cleaned = path.strip()
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    if not cleaned.startswith("/api/") and cleaned != "/api":
        cleaned = "/api" + cleaned
    return cleaned


class _HttpBenchClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str,
        app: FastAPI | None = None,
    ) -> None:
        self._token = token
        self._app = app
        self._base_url = base_url
        self._client: httpx2.AsyncClient | None = None
        self._signed_in = False
        self._csrf_token: str | None = None

    def _get_client(self) -> httpx2.AsyncClient:
        if self._client is None:
            if self._app is not None:
                services = getattr(self._app.state, "services", None)
                port = services.settings.port if services is not None else 8765
                base_url = f"http://127.0.0.1:{port}"
                transport = httpx2.ASGITransport(app=self._app)
            else:
                base_url = self._base_url or "http://127.0.0.1:8765"
                transport = None
            self._client = httpx2.AsyncClient(transport=transport, base_url=base_url)
        return self._client

    async def ensure_signed_in(self) -> None:
        if self._signed_in:
            return
        client = self._get_client()
        url = "/api/auth/token"
        try:
            response = await client.post(url, json={"token": self._token})
        except Exception as exc:
            raise BenchHttpError(url, 500) from exc

        if response.status_code >= 400:
            raise BenchHttpError(url, response.status_code)

        csrf = response.cookies.get("csrf_token") or client.cookies.get("csrf_token")
        if csrf:
            self._csrf_token = csrf
        self._signed_in = True

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        await self.ensure_signed_in()
        route = _normalize_path(path)
        client = self._get_client()
        try:
            query = dict(params) if params is not None else None
            response = await client.get(route, params=query)
        except Exception as exc:
            if isinstance(exc, BenchHttpError):
                raise
            raise BenchHttpError(route, 500) from exc

        if response.status_code >= 400:
            raise BenchHttpError(route, response.status_code)

        return response.json() if response.content else None

    async def post(self, path: str, body: Mapping[str, Any]) -> Any:
        await self.ensure_signed_in()
        route = _normalize_path(path)
        client = self._get_client()
        headers: dict[str, str] = {}
        csrf = client.cookies.get("csrf_token") or self._csrf_token
        if csrf:
            headers["X-CSRF-Token"] = csrf

        try:
            response = await client.post(route, json=dict(body), headers=headers)
        except Exception as exc:
            if isinstance(exc, BenchHttpError):
                raise
            raise BenchHttpError(route, 500) from exc

        if response.status_code >= 400:
            raise BenchHttpError(route, response.status_code)

        return response.json() if response.content else None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


async def sign_in(base_url: str) -> BenchClient:
    """Signs in to the app at base_url using MLOPS_AGENTS_ACCESS_TOKEN."""
    token = os.environ.get("MLOPS_AGENTS_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Missing required environment variable: MLOPS_AGENTS_ACCESS_TOKEN")
    client = _HttpBenchClient(base_url=base_url.rstrip("/"), token=token)
    await client.ensure_signed_in()
    return client


def in_process(app: FastAPI, token: str) -> BenchClient:
    """Creates an in-process BenchClient for the FastAPI app via ASGITransport."""
    return _HttpBenchClient(app=app, token=token)
