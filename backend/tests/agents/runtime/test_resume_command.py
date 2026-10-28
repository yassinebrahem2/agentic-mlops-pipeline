from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.message import add_messages  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command, interrupt

from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.storage.sqlite import connect_checkpointer


class _TwoNodeState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


async def test_resume_update_replaces_the_checkpointed_system_message_in_place(
    tmp_path: Path,
) -> None:
    """The plan's premise: `update` given with `resume` on an interrupted node replaces the
    checkpointed message that has the same id, without moving it or adding a second one."""
    seen_after_resume: list[list[BaseMessage]] = []

    async def model(state: _TwoNodeState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="calling a tool")]}

    async def wait(state: _TwoNodeState) -> dict[str, Any]:
        interrupt(None)
        seen_after_resume.append(list(state["messages"]))
        return {}

    builder = StateGraph(_TwoNodeState)
    builder.add_node("model", model)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    builder.add_node("wait", wait)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    builder.add_edge(START, "model")
    builder.add_edge("model", "wait")
    builder.add_edge("wait", END)

    conn = await connect_checkpointer(tmp_path / "project.db")
    try:
        saver = turn_checkpointer(conn)
        await saver.setup()
        graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile parameters are partially untyped
        config: RunnableConfig = {"configurable": {"thread_id": "trn_1"}}

        await graph.ainvoke(  # pyright: ignore[reportUnknownMemberType]
            {"messages": [SystemMessage(content="old hook text"), HumanMessage(content="hi")]},
            config=config,
        )
        checkpoint = await saver.aget_tuple(config)
        assert checkpoint is not None
        before: list[BaseMessage] = checkpoint.checkpoint["channel_values"]["messages"]
        first_id = before[0].id
        assert first_id is not None

        await graph.ainvoke(  # pyright: ignore[reportUnknownMemberType]
            Command(
                resume=True,
                update={"messages": [SystemMessage(content="new hook text", id=first_id)]},
            ),
            config=config,
        )
    finally:
        await conn.close()

    assert len(seen_after_resume) == 1
    after = seen_after_resume[0]
    assert [m.id for m in after] == [m.id for m in before]
    assert [type(m) for m in after] == [SystemMessage, HumanMessage, AIMessage]
    assert after[0].content == "new hook text"
    assert sum(isinstance(m, SystemMessage) for m in after) == 1
