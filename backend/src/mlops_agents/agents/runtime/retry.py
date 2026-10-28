"""Try again on a failed turn (Decision #400): a turn that failed because every key of its
provider was exhausted runs again as a new turn on the same trigger message. A turn's trigger is
not always the user's message (another agent's message, a wake-up), so the turn is re-run, never
re-sent: no message is added and the trigger keeps its author."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.errors import DomainError

__all__ = ["retry_failed_turn"]


async def retry_failed_turn(
    project_id: str,
    session_id: str,
    agent: str,
    turn_id: str,
    *,
    store: SessionStore,
    settings: AgentSettingsService,
    caller: ModelCaller,
    lock: AsyncRLock,
    busy: Callable[[str, str], Awaitable[bool]],
    start_worker: Callable[[], None],
) -> None:
    """Under the session lock: only the session's latest turn, failed with `keys_exhausted`,
    runs again, so an older turn never re-runs out of order and a second click finds the new
    queued turn latest. Try again clears the provider's key marks (spec section 2), then the
    new queued turn starts as a sent message's does. Nothing is published: no message changes,
    and the turn's agent message is announced when the turn starts."""
    async with lock:
        turn = await store.stored_turn(project_id, turn_id)
        if turn is None or turn.session_id != session_id:
            raise DomainError("turn_not_found", "This turn does not exist.", status=404)
        retryable = (
            turn.status == "failed"
            and turn.error_kind == "keys_exhausted"
            and await store.latest_turn_id(project_id, session_id) == turn_id
        )
        if not retryable:
            raise DomainError(
                "turn_not_retryable",
                "Only the last turn, failed because no key of its provider worked, "
                "can be tried again.",
                status=409,
            )
        model, _ = await settings.resolve(project_id, agent)
        await caller.reset_keys(model.provider_id)
        await store.add_turn(project_id, session_id, turn.trigger_message_id, "queued")
        if not await busy(project_id, session_id):
            start_worker()
