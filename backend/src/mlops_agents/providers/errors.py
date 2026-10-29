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


# Beyond a day a Retry-After is not a usable wait (and would overflow a timedelta).
_MAX_RETRY_AFTER_S = 86400.0


def _retry_after(response: httpx2.Response) -> float | None:
    """The Retry-After seconds, or None when absent, malformed, non-positive or absurdly large."""
    value = response.headers.get("retry-after")
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    if seconds is None or not 0 < seconds <= _MAX_RETRY_AFTER_S:
        return None
    return seconds


_BAD_GENERATION_CODES = ("tool_use_failed", "json_validate_failed")
_BILLING_CODE = "insufficient_quota"
_PAYMENT_REQUIRED = 402


def _code(e: openai.APIError) -> object:
    """The error code: the attribute, else the body's `code`, else its nested `error.code`."""
    code: object = getattr(e, "code", None)
    body = getattr(e, "body", None)
    if not code and isinstance(body, dict):
        body_dict = cast(dict[str, Any], body)
        code = body_dict.get("code")
        if not code:
            nested_err = body_dict.get("error")
            if isinstance(nested_err, dict):
                nested_dict = cast(dict[str, Any], nested_err)
                code = nested_dict.get("code")
    return code


def _kind(exc: BaseException) -> tuple[ProviderErrorKind, float | None]:
    for e in _chain(exc):
        if isinstance(e, openai.APIError):
            code = _code(e)
            if code in _BAD_GENERATION_CODES:
                return ProviderErrorKind.BAD_GENERATION, None
            # Only an explicit billing signal is quota: providers word per-minute 429s as
            # "quota exceeded" too, and those are rate limits.
            if code == _BILLING_CODE:
                return ProviderErrorKind.QUOTA, None

        if isinstance(e, openai.AuthenticationError | openai.PermissionDeniedError):
            return ProviderErrorKind.AUTH, None
        if isinstance(e, openai.RateLimitError):
            return ProviderErrorKind.RATE_LIMIT, _retry_after(e.response)
        if isinstance(e, openai.APIConnectionError | httpx2.TransportError):
            return ProviderErrorKind.UNREACHABLE, None
        if isinstance(e, openai.APIStatusError):
            # The openai SDK has no class for 402; it raises a plain APIStatusError.
            if e.status_code == _PAYMENT_REQUIRED:
                return ProviderErrorKind.QUOTA, None
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
