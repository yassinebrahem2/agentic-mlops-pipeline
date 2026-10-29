"""Waiting turns lifecycle and handlers (I9, I12).

Owned by the approvals plan (shared with Task 4 and Task 5).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mlops_agents.agents.approvals.decisions import settle_decisions
from mlops_agents.agents.approvals.store import ApprovalStore, DecidedBy
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageStatus,
    Part,
    ToolCallPart,
    TurnEnd,
)
from mlops_agents.agents.runtime.graph import TurnSink, turn_checkpointer
from mlops_agents.agents.runtime.hooks import GateDecision, TurnObserver
from mlops_agents.agents.runtime.hub import SessionHub
from mlops_agents.agents.runtime.stall import StallState, stall_part
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.sqlite import connect_checkpointer

if TYPE_CHECKING:
    from mlops_agents.agents.runtime.executor import TurnExecutor, TurnOutcome

logger = logging.getLogger(__name__)

# How long one observer call may take before it is abandoned and logged.
OBSERVER_TIMEOUT_S = 10.0

__all__ = [
    "OBSERVER_TIMEOUT_S",
    "UNEXPECTED_FAILURE_TEXT",
    "_UNEXPECTED_FAILURE",
    "_CheckpointHolder",
    "_Ending",
    "_ending_of",
    "_observed",
    "cancel_turn_approvals",
    "delete_checkpoint_thread",
    "finish_waiting_message",
    "handle_turn_waiting",
    "open_checkpointer",
    "stop_waiting_turn",
]


class _CheckpointHolder:
    def __init__(self, saver: AsyncSqliteSaver) -> None:
        self.saver = saver
        self.preserved = False


@asynccontextmanager
async def open_checkpointer(db_path: str | Path, turn_id: str) -> AsyncGenerator[_CheckpointHolder]:
    conn = await connect_checkpointer(Path(db_path))
    try:
        saver = turn_checkpointer(conn)
        await saver.setup()
        holder = _CheckpointHolder(saver)
        try:
            yield holder
        finally:
            if not holder.preserved:
                try:
                    await saver.adelete_thread(turn_id)
                except Exception:
                    logger.exception("Could not delete checkpointer thread for turn %s", turn_id)
    finally:
        # Windows keeps the project folder locked until the connection is closed, also when a
        # second cancel interrupts the thread cleanup.
        await conn.close()


async def delete_checkpoint_thread(db_path: str | Path, turn_id: str) -> None:
    """Delete a turn's checkpoint thread from the SQLite checkpointer."""
    conn = await connect_checkpointer(Path(db_path))
    try:
        saver = turn_checkpointer(conn)
        await saver.setup()
        try:
            await saver.adelete_thread(turn_id)
        except Exception:
            logger.exception("Could not delete checkpointer thread for turn %s", turn_id)
    finally:
        await conn.close()


@dataclass(frozen=True)
class _Ending:
    """How a taken turn ended, or that it waits for approval, decided before anything of it is
    stored. `failure_notice` is the system message of a failure still to be written: None when
    there is none left to write (no failure, written already, or recorded by the executor)."""

    status: TurnEnd | Literal["waiting"]
    error: str | None = None
    failure_notice: str | None = None


UNEXPECTED_FAILURE_TEXT = "The turn failed unexpectedly. The details are in the server log."
# A failure no part of the turn recorded: its system message is written with the end.
_UNEXPECTED_FAILURE = _Ending("failed", UNEXPECTED_FAILURE_TEXT, UNEXPECTED_FAILURE_TEXT)


def _ending_of(turn_id: str, child_task: asyncio.Task[TurnOutcome]) -> _Ending:
    """How a finished, not cancelled turn task ended; an error that escaped it fails the turn."""
    error = child_task.exception()
    if error is not None:
        logger.error("Turn %s failed outside the executor", turn_id, exc_info=error)
        return _UNEXPECTED_FAILURE
    end_status, end_error = child_task.result()
    return _Ending(end_status, end_error)


async def _observed(call: Awaitable[None]) -> None:
    """Await one observer call, bounded. A failing or hanging observer is logged and never
    stops the queue. The end report runs the observers one after another through a cancel,
    so hanging ones hold a shutdown up to OBSERVER_TIMEOUT_S each."""
    try:
        async with asyncio.timeout(OBSERVER_TIMEOUT_S):
            await call
    except asyncio.CancelledError:
        # Only a cancel of the worker itself goes on; an observer's own cancel is its failure.
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
        logger.exception("A turn observer was cancelled")
    except Exception:
        logger.exception("A turn observer failed")


async def handle_turn_waiting(
    *,
    store: SessionStore,
    sink: TurnSink,
    project_id: str,
    session_id: str,
    agent: str,
    turn_id: str,
    message_id: str | None,
    current_parts: list[Part],
    decisions: dict[str, GateDecision] | None = None,
    stall: StallState | None = None,
    lock: AbstractAsyncContextManager[Any],
    observers: Sequence[TurnObserver] = (),
) -> None:
    """Transition a turn and its agent message to 'waiting' state, emit TurnWaiting, and notify observers."""
    existing_apr_ids = {p.approval_id for p in current_parts if isinstance(p, ApprovalPart)}
    if decisions:
        for tool_call_id, dec in decisions.items():
            if (
                dec.outcome == "ask"
                and dec.approval_id is not None
                and dec.preview is not None
                and dec.approval_id not in existing_apr_ids
            ):
                apr_part = ApprovalPart(
                    type="approval",
                    approval_id=dec.approval_id,
                    tool_call_id=tool_call_id,
                    preview=dec.preview,
                    status="pending",
                    note=None,
                    proposal_kind=dec.proposal_kind,
                )
                insert_idx = None
                for idx, part in enumerate(current_parts):
                    if isinstance(part, ToolCallPart) and part.id == tool_call_id:
                        insert_idx = idx + 1
                        break
                if insert_idx is not None:
                    current_parts.insert(insert_idx, apr_part)
                else:
                    current_parts.append(apr_part)
                existing_apr_ids.add(dec.approval_id)
    if stall is not None and stall.approval_id not in existing_apr_ids:
        current_parts.append(stall_part(stall))

    if message_id is not None:
        async with lock:
            updated_msg = await store.update_message(
                project_id, message_id, parts=current_parts, status="waiting"
            )
            await sink.message(updated_msg, created=False)

    await store.set_turn(project_id, turn_id, "waiting")

    await sink.turn_waiting(turn_id)

    for observer in observers:
        await _observed(observer.turn_waiting(project_id, agent, turn_id))


async def cancel_turn_approvals(
    project_id: str,
    session_id: str,
    turn_id: str,
    *,
    decided_by: DecidedBy,
    store: SessionStore,
    hub: SessionHub,
    lock: AbstractAsyncContextManager[Any],
    approval_store: ApprovalStore,
    audit_log: AuditLog | None,
    event_log: EventLog | None,
) -> None:
    """The one sweep of an ended turn: cancel its pending approvals (telling their observers),
    then write their records and cards. Each card is rewritten in the approval's own message
    and only where it exists (a turn that has not waited yet holds none); only that rewrite
    takes the session lock."""
    cancelled = await approval_store.cancel_for_turn(project_id, turn_id, decided_by=decided_by)

    async def rewrite_part(
        _approval_session: str, message_id: str, transform: Callable[[list[Part]], list[Part]]
    ) -> object:
        # Every approval of the turn is in the turn's session, whose hub and lock these are.
        async with lock:
            updated = await store.rewrite_parts(project_id, session_id, message_id, transform)
            if updated is not None:
                await hub.message(updated, created=False)
        return updated

    await settle_decisions(
        project_id,
        cancelled,
        store=approval_store,
        databases=store.databases,
        audit_log=audit_log,
        event_log=event_log,
        rewrite_part=rewrite_part,
    )


async def finish_waiting_message(
    project_id: str,
    session_id: str,
    turn_id: str,
    status: MessageStatus,
    *,
    store: SessionStore,
    hub: SessionHub,
    lock: AbstractAsyncContextManager[Any],
) -> None:
    """Give the ended turn's agent message its final status when it is still `waiting`: a turn
    that ends without running again (a Stop before its worker ran, a failed write of its wait)
    would otherwise keep it."""
    async with lock:
        message = await store.turn_agent_message(project_id, session_id, turn_id)
        if message is not None and message.status == "waiting":
            updated = await store.update_message(project_id, message.id, status=status)
            await hub.message(updated, created=False)


async def stop_waiting_turn(
    *,
    project_id: str,
    session_id: str,
    agent: str,
    turn_id: str,
    store: SessionStore,
    hub: SessionHub,
    executor: TurnExecutor,
    approval_store: ApprovalStore,
    audit_log: AuditLog | None = None,
    event_log: EventLog | None = None,
    observers: Sequence[TurnObserver] = (),
) -> None:
    """Stop a waiting turn: store it stopped first, so no decision commits for it any more and
    a crash before the sweep leaves pending approvals that recovery expires; then the sweep,
    the stopped message, the end's publish, the checkpoint and the observers. The caller holds
    the session lock (so nothing here takes it again)."""
    await store.set_turn(project_id, turn_id, "stopped")
    held_by_caller = nullcontext()
    await cancel_turn_approvals(
        project_id,
        session_id,
        turn_id,
        decided_by="user",
        store=store,
        hub=hub,
        lock=held_by_caller,
        approval_store=approval_store,
        audit_log=audit_log,
        event_log=event_log,
    )
    await finish_waiting_message(
        project_id, session_id, turn_id, "stopped", store=store, hub=hub, lock=held_by_caller
    )
    await hub.turn_ended(turn_id, status="stopped", error=None)

    await executor.delete_checkpoint(project_id, turn_id)

    for observer in observers:
        await _observed(observer.turn_ended(project_id, agent, turn_id, "stopped", None))
