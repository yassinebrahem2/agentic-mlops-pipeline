"""Key failover: one shared record of each provider key's rate limits and exhausted marks, per
model, and the call loop that moves between keys (stage 3 spec, section 2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel

from mlops_agents.providers.errors import ProviderCallError, ProviderErrorKind, classify
from mlops_agents.providers.models import Provider

logger = logging.getLogger(__name__)

TRANSIENT_BACKOFFS = (1.0, 2.0)
# A per-minute limit has cleared a minute after it was hit: a key limited again after that
# reached its daily limit. It also bounds every wait, so a call never sleeps longer.
LIMIT_WINDOW = timedelta(minutes=1)

KeyStatus = Literal["ok", "limited", "exhausted_daily", "billing", "rejected"]
Exhaustion = Literal["exhausted_daily", "billing", "rejected"]

_SAME_KEY_RETRY = (ProviderErrorKind.TRANSIENT, ProviderErrorKind.UNREACHABLE)
_EXHAUSTS: dict[ProviderErrorKind, Exhaustion] = {
    ProviderErrorKind.QUOTA: "billing",
    ProviderErrorKind.AUTH: "rejected",
}
_NEXT_KEY = (ProviderErrorKind.RATE_LIMIT, *_EXHAUSTS)


class KeyState(BaseModel):
    key_index: int
    status: KeyStatus
    since: datetime | None  # when the key became exhausted, or was last limited; None when ok


class AllKeysExhausted(Exception):
    def __init__(self, keys: list[KeyState]) -> None:
        super().__init__("Every key of the provider is exhausted for this model.")
        self.keys = keys


class RateLimited(Exception):
    """Raised instead of waiting when the call was made with `wait=False`."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"Every usable key is rate limited; one clears in {retry_after_s:.0f} s.")
        self.retry_after_s = retry_after_s


class StreamedTextFailure(Exception):
    """Raised by an attempt that failed after text streamed: it is never failed over."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("The model call failed after its text had started to stream.")
        self.cause = cause


class Clock(Protocol):
    def now(self) -> datetime: ...  # aware, UTC

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class _Exhausted:
    status: Exhaustion
    since: datetime
    probe_at: datetime | None = None  # pushed one cooldown ahead each time a probe is taken

    def probe_time(self, cooldown: timedelta) -> datetime:
        due = self.since + cooldown
        return due if self.probe_at is None else max(due, self.probe_at)


@dataclass
class _Entry:
    last_limited: datetime | None = None
    clear_at: datetime | None = None
    exhausted: _Exhausted | None = None


@dataclass(frozen=True)
class _Pick:
    index: int
    probe: bool


_LINES: dict[KeyStatus, str] = {
    "exhausted_daily": "daily limit reached (since {since})",
    "billing": "billing, out of credit",
    "rejected": "rejected by the provider",
    "limited": "rate limited (since {since})",
    "ok": "ok",
}


class KeyFailover:
    """One per app, shared by every call. Each read-then-write of the state is one block with
    no `await` inside, so concurrent calls need no lock. `on_success` is the one owner of the
    success rule: a provider marked needs attention answered, so the mark is cleared
    (Decision #384)."""

    def __init__(
        self,
        clock: Clock | None = None,
        on_success: Callable[[Provider], Awaitable[None]] | None = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._on_success = on_success
        self._entries: dict[tuple[str, int, str], _Entry] = {}
        self._cursors: dict[tuple[str, str], int] = {}

    async def call[T](
        self,
        provider: Provider,
        model: str,
        attempt: Callable[[int | None], Awaitable[T]],
        *,
        cooldown_minutes: int,
        wait: bool = True,
    ) -> T:
        if not provider.keys:
            result = await attempt(None)
            await self._report_success(provider)
            return result
        cooldown = timedelta(minutes=cooldown_minutes)
        transient_retries = 0
        pick: _Pick | None = None
        while True:
            if pick is None:
                pick = self._pick(provider, model, cooldown)
                if pick is None:
                    delay = self._delay(provider, model, cooldown)
                    if not wait:
                        raise RateLimited(retry_after_s=delay)
                    await self._clock.sleep(delay)
                    continue
            try:
                result = await attempt(pick.index)
            except StreamedTextFailure as failure:
                self._record(provider, model, pick, classify(failure.cause))
                raise
            except Exception as exc:
                error = classify(exc)
                if error.kind in _SAME_KEY_RETRY and transient_retries < len(TRANSIENT_BACKOFFS):
                    await self._clock.sleep(TRANSIENT_BACKOFFS[transient_retries])
                    transient_retries += 1
                    continue
                if error.kind not in _NEXT_KEY:
                    raise
                self._record(provider, model, pick, error)
                pick = None
                continue
            self._succeed(provider, model, pick.index)
            await self._report_success(provider)
            return result

    def states(self, provider: Provider, model: str) -> list[KeyState]:
        now = self._clock.now()
        return [
            self._state(index, self._entries.get((provider.id, index, model)), now)
            for index in range(len(provider.keys))
        ]

    def clear(self, provider: Provider) -> None:
        """Forget every key and model of the provider: Try again, or its keys changed."""
        self._entries = {k: v for k, v in self._entries.items() if k[0] != provider.id}
        self._cursors = {k: v for k, v in self._cursors.items() if k[0] != provider.id}

    def describe_keys(self, states: list[KeyState]) -> str:
        """The one wording of the keys' states. Keys are named by position, never by value."""
        lines: list[str] = []
        for state in states:
            since = f"{state.since:%H:%M} UTC" if state.since is not None else ""
            line = _LINES[state.status].format(since=since)
            lines.append(f"Key {state.key_index + 1}: {line}.")
        return " ".join(lines)

    async def _report_success(self, provider: Provider) -> None:
        """The call already succeeded: a failing `on_success` is logged, never raised."""
        if self._on_success is None or provider.status != "needs_attention":
            return
        try:
            await self._on_success(provider)
        except Exception:
            logger.exception("The success hook of provider %s failed.", provider.id)

    def _entry(self, provider: Provider, index: int, model: str) -> _Entry:
        return self._entries.setdefault((provider.id, index, model), _Entry())

    def _order(self, provider: Provider, model: str) -> list[int]:
        count = len(provider.keys)
        start = self._cursors.get((provider.id, model), 0) % count
        return [(start + offset) % count for offset in range(count)]

    def _pick(self, provider: Provider, model: str, cooldown: timedelta) -> _Pick | None:
        now = self._clock.now()
        order = self._order(provider, model)
        for index in order:
            exhausted = self._entry(provider, index, model).exhausted
            if exhausted is not None and now >= exhausted.probe_time(cooldown):
                # Taking the probe pushes the next one a cooldown ahead: one call probes at a time.
                exhausted.probe_at = now + cooldown
                return _Pick(index, probe=True)
        for index in order:
            entry = self._entry(provider, index, model)
            if entry.exhausted is None and (entry.clear_at is None or now >= entry.clear_at):
                return _Pick(index, probe=False)
        return None

    def _delay(self, provider: Provider, model: str, cooldown: timedelta) -> float:
        """Seconds until a key can be tried, when none can now; raises when every key is
        exhausted."""
        now = self._clock.now()
        entries = [self._entry(provider, index, model) for index in range(len(provider.keys))]
        if all(entry.exhausted is not None for entry in entries):
            raise AllKeysExhausted(self.states(provider, model))
        times = [
            entry.exhausted.probe_time(cooldown) if entry.exhausted is not None else entry.clear_at
            for entry in entries
        ]
        earliest = min(time for time in times if time is not None)
        return min(earliest - now, LIMIT_WINDOW).total_seconds()

    def _record(
        self, provider: Provider, model: str, pick: _Pick, error: ProviderCallError
    ) -> None:
        """Apply a failed attempt's state change and move the cursor past the key."""
        now = self._clock.now()
        entry = self._entry(provider, pick.index, model)
        if error.kind is ProviderErrorKind.RATE_LIMIT:
            # A failed probe stays exhausted; its next probe time is already pushed.
            if not pick.probe:
                self._limit(entry, error.retry_after, now)
        elif error.kind in _EXHAUSTS:
            self._exhaust(entry, _EXHAUSTS[error.kind], now)
        else:
            return
        self._cursors[(provider.id, model)] = (pick.index + 1) % len(provider.keys)

    def _limit(self, entry: _Entry, retry_after: float | None, now: datetime) -> None:
        if entry.last_limited is not None and now - entry.last_limited >= LIMIT_WINDOW:
            self._exhaust(entry, "exhausted_daily", now)
            return
        wait = timedelta(seconds=retry_after or LIMIT_WINDOW.total_seconds())
        entry.last_limited = now
        entry.clear_at = now + min(wait, LIMIT_WINDOW)

    def _exhaust(self, entry: _Entry, status: Exhaustion, now: datetime) -> None:
        entry.last_limited = None
        entry.clear_at = None
        if entry.exhausted is None:
            entry.exhausted = _Exhausted(status, since=now)
        else:
            entry.exhausted.status = status

    def _succeed(self, provider: Provider, model: str, index: int) -> None:
        self._entries.pop((provider.id, index, model), None)
        self._cursors[(provider.id, model)] = index

    @staticmethod
    def _state(index: int, entry: _Entry | None, now: datetime) -> KeyState:
        if entry is not None and entry.exhausted is not None:
            return KeyState(
                key_index=index, status=entry.exhausted.status, since=entry.exhausted.since
            )
        if entry is not None and entry.clear_at is not None and now < entry.clear_at:
            return KeyState(key_index=index, status="limited", since=entry.last_limited)
        return KeyState(key_index=index, status="ok", since=None)
