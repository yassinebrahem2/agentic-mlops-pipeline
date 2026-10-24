"""Inbound messages, the turn-end check and the step budget in the turn graph."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.hook_fakes import (
    EchoTool,
    ScriptedInbound,
    ScriptedTurnEnd,
    echo,
    run_graph,
    text_reply,
    tool_reply,
)


async def test_inbound_messages_follow_the_tool_results_and_stay_in_later_requests(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    delivered = HumanMessage(content="From coder: done")
    inbound = ScriptedInbound([[], [delivered]])
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a")), tool_reply(echo("c2", "b")), text_reply("ok")],
        tools=(EchoTool(),),
        inbound=inbound,
    )

    first, second, third = run.caller.requests
    assert all(m.content != delivered.content for m in first)
    # Delivered at the next model step, right after the previous step's tool results.
    assert isinstance(second[-2], ToolMessage)
    assert second[-1].content == delivered.content
    assert [m.content for m in third].count(delivered.content) == 1
    assert inbound.takes == 3
    assert [m.content for m in run.state["messages"]].count(delivered.content) == 1


async def test_a_turn_end_note_runs_exactly_one_more_model_step(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    note = "Your task 't' has changes that are not in a pull request."
    check = ScriptedTurnEnd(note)
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [text_reply("first end"), text_reply("second end")],
        tools=(EchoTool(),),
        turn_end=check,
    )

    assert check.asked == 1
    assert len(run.caller.requests) == 2
    extra = run.caller.requests[1]
    assert isinstance(extra[-1], HumanMessage)
    assert extra[-1].content == note
    assert isinstance(extra[-2], AIMessage)
    # The note is in that one request only: never in the turn's state.
    assert all(m.content != note for m in run.state["messages"])
    texts = [p.text for p in run.parts if isinstance(p, TextPart)]
    assert texts == ["first end", "second end"]


async def test_no_note_means_no_extra_step(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    check = ScriptedTurnEnd(None)
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a")), text_reply("done")],
        tools=(EchoTool(),),
        turn_end=check,
    )

    assert check.asked == 1
    assert len(run.caller.requests) == 2


async def test_a_note_step_that_calls_tools_is_not_noted_again(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    check = ScriptedTurnEnd("Open a pull request.")
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [text_reply("end"), tool_reply(echo("c1", "a")), text_reply("end again")],
        tools=(EchoTool(),),
        turn_end=check,
    )

    assert check.asked == 1
    assert len(run.caller.requests) == 3


async def test_many_tool_rounds_fit_and_max_steps_still_counts_model_steps(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    rounds = 40
    replies = [tool_reply(echo(f"c{i}", str(i))) for i in range(rounds)]
    tool = EchoTool()
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [*replies, text_reply("done")],
        tools=(tool,),
        max_steps=None,
    )
    assert len(tool.ran) == rounds
    assert run.state["step_count"] == rounds + 1

    limited = EchoTool()
    capped = await run_graph(
        tmp_path / "capped",
        paths.bin,
        databases,
        [tool_reply(echo(f"d{i}", str(i))) for i in range(5)],
        tools=(limited,),
        max_steps=3,
    )
    assert len(capped.caller.requests) == 3
    assert len(limited.ran) == 3
    assert capped.state["stopped_reason"] == "max_steps"
