from __future__ import annotations

from typing import Any, TypedDict, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.hooks import GateDecision
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import (
    ReadTool,
    RunTool,
    WriteTool,
    make_call,
    setup_session_and_turn,
)


class _GateState(TypedDict):
    decisions: dict[str, GateDecision]


@pytest.mark.asyncio
async def test_gate_wait_pending_triggers_interrupt_in_graph(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_gatewaitinterrupt"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_write = WriteTool()
    call = make_call(t_write, "c_wait_1", project_id)
    decisions = await gate.decide([call])

    # Build a LangGraph test graph with checkpointer
    builder = StateGraph(_GateState)

    async def wait_node(state: _GateState) -> _GateState:
        result = await gate.wait(project_id, "trn_1", state["decisions"])
        return {"decisions": result}

    builder.add_node("wait_step", wait_node)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "wait_step")
    checkpointer = MemorySaver()
    graph = builder.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]

    config: RunnableConfig = {"configurable": {"thread_id": "test_thread_1"}}
    # When approval is pending, wait triggers interrupt
    res = cast(
        dict[str, Any],
        await graph.ainvoke({"decisions": decisions}, config=config),  # pyright: ignore[reportUnknownMemberType]
    )
    assert "__interrupt__" in res


@pytest.mark.asyncio
async def test_gate_wait_decided_returns_mapped_decisions(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_gatewaitdecided"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_write = WriteTool()
    t_run = RunTool()

    calls = [
        make_call(t_write, "c_w_approved", project_id),
        make_call(t_run, "c_r_rejected", project_id),
    ]
    decisions = await gate.decide(calls)

    apr_w_id = decisions["c_w_approved"].approval_id
    apr_r_id = decisions["c_r_rejected"].approval_id
    assert apr_w_id is not None
    assert apr_r_id is not None

    # The turn waits for them; decide both: approve first, reject second with note
    await SessionStore(databases).set_turn(project_id, "trn_1", "waiting")
    await store.decide(project_id, apr_w_id, approve=True)
    await store.decide(project_id, apr_r_id, approve=False, note="dangerous command")

    # In a graph, calling wait now returns the updated decisions
    builder = StateGraph(_GateState)

    async def wait_node(state: _GateState) -> _GateState:
        result = await gate.wait(project_id, "trn_1", state["decisions"])
        return {"decisions": result}

    builder.add_node("wait_step", wait_node)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "wait_step")
    graph = builder.compile(checkpointer=MemorySaver())  # pyright: ignore[reportUnknownMemberType]

    config: RunnableConfig = {"configurable": {"thread_id": "test_thread_2"}}
    res = cast(
        dict[str, Any],
        await graph.ainvoke({"decisions": decisions}, config=config),  # pyright: ignore[reportUnknownMemberType]
    )

    assert "__interrupt__" not in res
    result_decisions = cast(dict[str, GateDecision], res["decisions"])
    assert result_decisions["c_w_approved"].outcome == "approved"
    assert result_decisions["c_w_approved"].message is None

    assert result_decisions["c_r_rejected"].outcome == "rejected"
    assert (
        result_decisions["c_r_rejected"].message
        == "The user rejected this action. Note: dangerous command"
    )


@pytest.mark.asyncio
async def test_gate_wait_no_asks_returns_immediately(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_gatewaitnoasks"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "bypass")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_read = ReadTool()
    call = make_call(t_read, "c_read_bypass", project_id)
    decisions = await gate.decide([call])

    res = await gate.wait(project_id, "trn_1", decisions)
    assert res == decisions
