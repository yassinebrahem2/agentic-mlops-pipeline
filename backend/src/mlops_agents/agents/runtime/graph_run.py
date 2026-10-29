"""One run of a turn's graph, from a new turn's state or a resume, to its end or to a wait for
approval."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import Durability

from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import Part
from mlops_agents.agents.runtime.graph import TurnHooks, TurnSink, create_turn_graph
from mlops_agents.agents.runtime.hooks import GateDecision, TurnObserver
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.stall import StallState, pending_stall
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.waiting import handle_turn_waiting, open_checkpointer

logger = logging.getLogger(__name__)

__all__ = ["TURN_DURABILITY", "run_turn_graph"]

# "async" runs each checkpoint write as a background task that a cancel does not cancel, and the
# run's exit waits for it: a Stop or close_project never leaves a checkpoint transaction open,
# which would lock the project file. "sync" awaits the write in the turn's own task, where a
# cancel lands inside it.
TURN_DURABILITY: Durability = "async"


async def run_turn_graph(
    *,
    agent: Agent,
    caller: ModelCaller,
    store: SessionStore,
    hooks: TurnHooks,
    observers: Sequence[TurnObserver],
    project_id: str,
    session_id: str,
    turn_id: str,
    message_id: str,
    parts: list[Part],
    graph_input: Any,
    sink: TurnSink,
    lock: AbstractAsyncContextManager[Any],
) -> bool:
    """Run the turn graph from `graph_input`; True when the turn now waits for approval.

    A waiting turn keeps its checkpoint for the resume, and its wait is stored and told
    (`handle_turn_waiting`); any other run deletes the checkpoint thread.
    """
    is_interrupted = False
    decisions_to_record: dict[str, GateDecision] | None = None
    stall: StallState | None = None

    # The checkpointer connection is closed before the end state is written, so once a turn
    # reports how it ended, nothing of it still holds the project file.
    async with open_checkpointer(store.db_path(project_id), turn_id) as cp:
        graph = create_turn_graph(
            agent=agent,
            caller=caller,
            store=store,
            sink=sink,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            message_id=message_id,
            current_parts=parts,
            checkpointer=cp.saver,
            lock=lock,
            hooks=hooks,
        )
        config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
        try:
            res = await graph.ainvoke(  # pyright: ignore[reportUnknownMemberType]
                graph_input, config=config, durability=TURN_DURABILITY
            )
            if isinstance(res, dict) and bool(res.get("__interrupt__")):  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                is_interrupted = True
            else:
                chk_state = await graph.aget_state(config)
                if chk_state.tasks and any(t.interrupts for t in chk_state.tasks):
                    is_interrupted = True
        except GraphInterrupt:
            is_interrupted = True

        if is_interrupted:
            cp.preserved = True
            try:
                chk_state = await graph.aget_state(config)
                decisions_to_record = chk_state.values.get("decisions")
                stall = pending_stall(chk_state.values.get("stall"))
            except Exception:
                logger.debug(
                    "Could not read graph state decisions for turn %s", turn_id, exc_info=True
                )

    if is_interrupted:
        await handle_turn_waiting(
            store=store,
            sink=sink,
            project_id=project_id,
            session_id=session_id,
            agent=agent.definition.name,
            turn_id=turn_id,
            message_id=message_id,
            current_parts=parts,
            decisions=decisions_to_record,
            stall=stall,
            lock=lock,
            observers=observers,
        )
    return is_interrupted
