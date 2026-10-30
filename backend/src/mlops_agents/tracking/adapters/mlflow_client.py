"""MLflow HTTP client and request primitives."""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx2

from mlops_agents.credentials.scrub import error_text

logger = logging.getLogger(__name__)


class SyncAbortError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_SyncAbortError = SyncAbortError


class MlflowClient:
    """HTTP client wrapper for MLflow REST API."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    @property
    def http(self) -> httpx2.AsyncClient:
        return self._http

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._http.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                follow_redirects=False,
                timeout=30.0,
            )
        except httpx2.HTTPError as exc:
            raise _SyncAbortError(error_text(exc)) from exc

        if resp.status_code in (401, 403):
            raise _SyncAbortError("The tracker rejected the credential")
        if resp.status_code != 200:
            raise _SyncAbortError(f"MLflow answered with HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception as exc:
            raise _SyncAbortError("MLflow answered with data this app cannot read") from exc

        if not isinstance(data, dict):
            raise SyncAbortError("MLflow answered with data this app cannot read")

        return cast(dict[str, Any], data)
