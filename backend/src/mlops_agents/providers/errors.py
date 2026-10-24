from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Any, cast

import httpx2
import openai

from mlops_agents.credentials.scrub import error_text, scrub_values


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    UNREACHABLE = "unreachable"
    BAD_GENERATION = "bad_generation"
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
        if isinstance(e, openai.APIError):
            body = getattr(e, "body", None)
            code: object = getattr(e, "code", None)
            if not code and isinstance(body, dict):
                body_dict = cast(dict[str, Any], body)
                code = body_dict.get("code")
                if not code:
                    nested_err = body_dict.get("error")
                    if isinstance(nested_err, dict):
                        nested_dict = cast(dict[str, Any], nested_err)
                        code = nested_dict.get("code")
            if code in ("tool_use_failed", "json_validate_failed"):
                return ProviderErrorKind.BAD_GENERATION, None

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


def _provider_message(exc: BaseException) -> str | None:
    for e in _chain(exc):
        if isinstance(e, openai.APIError):
            body = getattr(e, "body", None)
            if isinstance(body, dict):
                body_dict = cast(dict[str, Any], body)
                msg = body_dict.get("message")
                if isinstance(msg, str) and msg:
                    return msg
                nested = body_dict.get("error")
                if isinstance(nested, dict):
                    nested_dict = cast(dict[str, Any], nested)
                    msg = nested_dict.get("message")
                    if isinstance(msg, str) and msg:
                        return msg
    return None


def classify(exc: BaseException, secrets: Iterable[str] = ()) -> ProviderCallError:
    kind, retry_after = _kind(exc)
    raw_message = _provider_message(exc) if kind is ProviderErrorKind.BAD_GENERATION else None
    if raw_message is None:
        raw_message = error_text(exc)
    return ProviderCallError(kind, scrub_values(raw_message, secrets), retry_after)
