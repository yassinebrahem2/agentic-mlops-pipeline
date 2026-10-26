"""Waiting turns lifecycle and handlers (I9, I12).

Owned by the approvals plan (shared with Task 4 and Task 5).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mlops_agents.agents.approvals.settings import get_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    Part,
    ToolCallPart,
    TurnEnd,
)
from mlops_agents.agents.runtime.graph import TurnSink, turn_checkpointer
from mlops_agents.agents.runtime.hooks import GateDecision, TurnObserver
from mlops_agents.agents.runtime.hub import SessionHub
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
    "delete_checkpoint_thread",
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
    lock: AbstractAsyncContextManager[Any],
    observers: Sequence[TurnObserver] = (),
) -> None:
    """Transition a turn and its agent message to 'waiting' state, emit TurnWaiting, and notify observers."""
    if decisions:
        existing_apr_ids = {p.approval_id for p in current_parts if isinstance(p, ApprovalPart)}
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

    if message_id is not None:
        async with lock:
            updated_msg = await store.update_message(
                project_id, message_id, parts=current_parts, status="waiting"
            )
            await sink.message(updated_msg, created=False)

    await store.set_turn(project_id, turn_id, "waiting")

    if hasattr(sink, "turn_waiting"):
        await sink.turn_waiting(turn_id)

    for observer in observers:
        await _observed(observer.turn_waiting(project_id, agent, turn_id))


async def stop_waiting_turn(
    *,
    project_id: str,
    session_id: str,
    agent: str,
    turn_id: str,
    store: SessionStore,
    hub: SessionHub,
    executor: TurnExecutor,
    audit_log: AuditLog | None = None,
    event_log: EventLog | None = None,
    observers: Sequence[TurnObserver] = (),
) -> None:
    """Stop a waiting turn: cancel approvals, mark message and turn stopped, delete checkpoint, notify."""
    approval_store = ApprovalStore(store.databases)
    cancelled = await approval_store.cancel_for_turn(project_id, turn_id)
    cancelled_ids = {a.id for a in cancelled}

    if cancelled:
        if audit_log is not None:
            level = await get_permission_level(store.databases, project_id)
            for apr in cancelled:
                await audit_log.record(
                    project_id,
                    actor="user",
                    action="approval",
                    risk_class=apr.risk_class,
                    permission_level=level,
                    decision="cancelled",
                    target=apr.tool,
                    detail={"approval_id": apr.id, "tool": apr.tool},
                )
        if event_log is not None:
            for apr in cancelled:
                await event_log.record(
                    project_id,
                    source="user",
                    type="approval.decided",
                    subject_id=apr.id,
                    payload={"approval_id": apr.id, "status": "cancelled"},
                )

    agent_msg = await store.turn_agent_message(project_id, session_id, turn_id)
    if agent_msg is not None:
        new_parts: list[Part] = []
        for part in agent_msg.parts:
            if isinstance(part, ApprovalPart) and part.approval_id in cancelled_ids:
                new_parts.append(part.model_copy(update={"status": "cancelled"}))
            else:
                new_parts.append(part)
        updated_msg = await store.update_message(
            project_id, agent_msg.id, parts=new_parts, status="stopped"
        )
        await hub.message(updated_msg, created=False)

    await store.set_turn(project_id, turn_id, "stopped")
    await hub.turn_ended(turn_id, status="stopped", error=None)

    await executor.delete_checkpoint(project_id, turn_id)

    for observer in observers:
        await _observed(observer.turn_ended(project_id, agent, turn_id, "stopped", None))
