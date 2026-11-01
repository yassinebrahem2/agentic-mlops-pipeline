"""The fake clock and the scripted provider calls the key failover tests drive."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx2
import openai

from mlops_agents.providers.failover import KeyState, KeyStatus
from mlops_agents.providers.models import KeyHint, Provider

START = datetime(2026, 10, 28, 14, 3, tzinfo=UTC)
KEY_VALUES = ("gsk_first_secret_value", "gsk_second_secret_value", "gsk_third_secret_value")
REQUEST = httpx2.Request("POST", "https://x.io/v1/chat/completions")


class FakeClock:
    """Time moves only when a test advances it or a call sleeps. `sleeps` records each sleep
    and `sleeping` is set once one starts; while `hold` is set, a sleep waits on it first, so
    a test can act during the sleep."""

    def __init__(self, start: datetime = START) -> None:
        self.current = start
        self.sleeps: list[float] = []
        self.sleeping = asyncio.Event()
        self.hold: asyncio.Event | None = None

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.sleeping.set()
        if self.hold is not None:
            await self.hold.wait()
        self.advance(seconds)
        await asyncio.sleep(0)


class Script:
    """An `attempt` that plays `outcomes` in order: an exception is raised, a string returned.
    `keys` records the key index of every attempt."""

    def __init__(self, *outcomes: BaseException | str) -> None:
        self._outcomes = list(outcomes)
        self.keys: list[int | None] = []

    async def __call__(self, key_index: int | None) -> str:
        self.keys.append(key_index)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def provider(key_count: int, provider_id: str = "prov_1") -> Provider:
    return Provider(
        id=provider_id,
        name="groq",
        kind="hosted",
        base_url="https://x.io/v1",
        status="ok",
        status_detail=None,
        keys=[KeyHint(position=i + 1, hint=KEY_VALUES[i]) for i in range(key_count)],
    )


def response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, request=REQUEST, headers=headers)


def rate_limit(retry_after: str | None = None) -> openai.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else None
    return openai.RateLimitError("quota exceeded", response=response(429, headers), body=None)


def billing() -> openai.RateLimitError:
    return openai.RateLimitError(
        "You exceeded your current quota",
        response=response(429),
        body={"code": "insufficient_quota"},
    )


def payment_required() -> openai.APIStatusError:
    return openai.APIStatusError("payment required", response=response(402), body=None)


def unauthorized() -> openai.AuthenticationError:
    return openai.AuthenticationError("bad key", response=response(401), body=None)


def forbidden() -> openai.PermissionDeniedError:
    return openai.PermissionDeniedError("forbidden", response=response(403), body=None)


def overloaded() -> openai.InternalServerError:
    return openai.InternalServerError("high demand", response=response(503), body=None)


def unreachable() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=REQUEST)


def bad_request() -> openai.BadRequestError:
    return openai.BadRequestError("bad model", response=response(400), body=None)


def bad_generation() -> openai.BadRequestError:
    return openai.BadRequestError(
        "Tool call validation failed", response=response(400), body={"code": "tool_use_failed"}
    )


def request_too_large() -> openai.BadRequestError:
    return openai.BadRequestError(
        "Context length exceeded",
        response=response(400),
        body={"error": {"code": "context_length_exceeded"}},
    )


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def statuses(states: list[KeyState]) -> list[KeyStatus]:
    return [state.status for state in states]
