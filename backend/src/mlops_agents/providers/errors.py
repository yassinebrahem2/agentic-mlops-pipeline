from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum

import httpx2
import openai

from mlops_agents.credentials.scrub import error_text, scrub_values


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    UNREACHABLE = "unreachable"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


class ProviderCallError(Exception):
    """A failed call to a model provider, classified. `message` is safe to show."""

    def __init__(
        self, kind: ProviderErrorKind, message: str, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """`exc` and its causes. langchain-openai wraps openai errors, so the type is in the chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _kind(exc: BaseException) -> tuple[ProviderErrorKind, float | None]:
    for e in _chain(exc):
        if isinstance(e, openai.AuthenticationError | openai.PermissionDeniedError):
            return ProviderErrorKind.AUTH, None
        if isinstance(e, openai.RateLimitError):
            body = str(e.body).lower() if e.body is not None else ""
            if "quota" in str(e).lower() or "insufficient_quota" in body:
                return ProviderErrorKind.QUOTA, None
            return ProviderErrorKind.RATE_LIMIT, _retry_after(e.response)
        if isinstance(e, openai.APIConnectionError | httpx2.TransportError):
            return ProviderErrorKind.UNREACHABLE, None
        if isinstance(e, openai.APIStatusError):
            if e.status_code >= 500:
                return ProviderErrorKind.TRANSIENT, None
            return ProviderErrorKind.BAD_REQUEST, None
    return ProviderErrorKind.UNKNOWN, None


def classify(exc: BaseException, secrets: Iterable[str] = ()) -> ProviderCallError:
    kind, retry_after = _kind(exc)
    return ProviderCallError(kind, scrub_values(error_text(exc), secrets), retry_after)
