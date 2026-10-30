from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, TypedDict

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.message import add_messages  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command, interrupt

from mlops_agents.agents.runtime import waiting
from mlops_agents.agents.runtime.agent import Agent, resume_command
from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.paths import DataPaths
from mlops_agents.storage.sqlite import connect_checkpointer
from tests.agents.runtime.graph_env import make_agent, prompt_messages
from tests.conftest import make_repo

TURN = "trn_1"
CONFIG: RunnableConfig = {"configurable": {"thread_id": TURN}}


class _TwoNodeState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _two_node_graph(
    saver: AsyncSqliteSaver, seen_after_resume: list[list[BaseMessage]], *, with_model: bool
) -> CompiledStateGraph[_TwoNodeState, None, _TwoNodeState, _TwoNodeState]:
    """A model node (unless left out), then a node that waits (as the approval gate does) and
    records what the resumed run sees."""

    async def model(state: _TwoNodeState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="calling a tool")]}

    async def wait(state: _TwoNodeState) -> dict[str, Any]:
        interrupt(None)
        seen_after_resume.append(list(state["messages"]))
        return {}

    builder = StateGraph(_TwoNodeState)
    builder.add_node("wait", wait)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    if with_model:
        builder.add_node("model", model)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
        builder.add_edge(START, "model")
        builder.add_edge("model", "wait")
    else:
        builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    return builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile parameters are partially untyped


async def _run(
    db_path: Path, graph_input: Any, seen: list[list[BaseMessage]], *, with_model: bool = True
) -> None:
    conn = await connect_checkpointer(db_path)
    try:
        saver = turn_checkpointer(conn)
        await saver.setup()
        graph = _two_node_graph(saver, seen, with_model=with_model)
        await graph.ainvoke(graph_input, config=CONFIG)  # pyright: ignore[reportUnknownMemberType]
    finally:
        await conn.close()


async def _checkpoint(db_path: Path) -> CheckpointTuple | None:
    conn = await connect_checkpointer(db_path)
    try:
        saver = turn_checkpointer(conn)
        await saver.setup()
        return await saver.aget_tuple(CONFIG)
    finally:
        await conn.close()


def _messages(checkpoint: CheckpointTuple | None) -> list[BaseMessage]:
    assert checkpoint is not None
    return list(checkpoint.checkpoint["channel_values"].get("messages", []))


@pytest.fixture
def agent(tmp_path: Path, paths: DataPaths, git_env: dict[str, str]) -> Agent:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    return replace(make_agent(repo_dir, paths.bin), hook_context="old hook text")


async def _start(db_path: Path, agent: Agent) -> list[BaseMessage]:
    """A turn interrupted while waiting, started as the executor starts one; its checkpoint's
    messages."""
    first = prompt_messages(
        agent.definition,
        agent.project,
        agent.layout,
        "Earlier.",
        [],
        hook_context=agent.hook_context,
    )
    await _run(db_path, {"messages": [*first, HumanMessage(content="hi")]}, [])
    return _messages(await _checkpoint(db_path))


async def test_resume_update_replaces_the_checkpointed_system_message_in_place(
    tmp_path: Path,
) -> None:
    """The plan's premise: `update` given with `resume` on an interrupted node replaces the
    checkpointed message that has the same id, without moving it or adding a second one."""
    db_path = tmp_path / "project.db"
    seen: list[list[BaseMessage]] = []
    await _run(
        db_path,
        {"messages": [SystemMessage(content="old hook text"), HumanMessage(content="hi")]},
        seen,
    )
    before = _messages(await _checkpoint(db_path))
    first_id = before[0].id
    assert first_id is not None

    update = {"messages": [SystemMessage(content="new hook text", id=first_id)]}
    await _run(db_path, Command(resume=True, update=update), seen)

    assert len(seen) == 1
    after = seen[0]
    assert [m.id for m in after] == [m.id for m in before]
    assert [type(m) for m in after] == [SystemMessage, HumanMessage, AIMessage]
    assert after[0].content == "new hook text"
    assert sum(isinstance(m, SystemMessage) for m in after) == 1


async def test_resume_command_keeps_the_thread_and_the_turn_completes_from_it(
    tmp_path: Path, agent: Agent
) -> None:
    db_path = tmp_path / "project.db"
    before = await _start(db_path, agent)

    command = await resume_command(
        replace(agent, hook_context="new hook text"), "Earlier.", db_path, TURN
    )

    assert await _checkpoint(db_path) is not None
    seen: list[list[BaseMessage]] = []
    await _run(db_path, command, seen)
    assert len(seen) == 1
    after = seen[0]
    assert [m.id for m in after] == [m.id for m in before]
    assert [type(m) for m in after] == [SystemMessage, HumanMessage, AIMessage]
    assert sum(isinstance(m, SystemMessage) for m in after) == 1
    system_text = str(after[0].content)
    assert system_text.endswith("\n\nnew hook text")
    assert "old hook text" not in system_text
    assert "Summary of the earlier conversation:\nEarlier." in system_text


async def test_a_checkpoint_read_that_raises_leaves_the_thread_intact(
    tmp_path: Path, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "project.db"
    await _start(db_path, agent)

    class FailingSaver(AsyncSqliteSaver):
        async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
            raise RuntimeError("checkpoint unreadable")

    def failing_checkpointer(conn: aiosqlite.Connection) -> AsyncSqliteSaver:
        return FailingSaver(conn)

    monkeypatch.setattr(waiting, "turn_checkpointer", failing_checkpointer)
    with pytest.raises(RuntimeError, match="checkpoint unreadable"):
        await resume_command(agent, None, db_path, TURN)
    monkeypatch.undo()

    assert await _checkpoint(db_path) is not None


async def test_a_checkpoint_without_messages_resumes_plain(tmp_path: Path, agent: Agent) -> None:
    db_path = tmp_path / "project.db"
    await _run(db_path, {"messages": []}, [], with_model=False)
    assert _messages(await _checkpoint(db_path)) == []

    assert await resume_command(agent, None, db_path, TURN) == Command(resume=True)
    assert await _checkpoint(db_path) is not None
    # A turn with no thread at all (nothing was checkpointed) resumes plain too.
    assert await resume_command(agent, None, tmp_path / "other.db", TURN) == Command(resume=True)
