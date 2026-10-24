"""The decide, wait and tools steps of the turn graph: the gate, parsing, previews, per-call context."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig

from mlops_agents.agents.runtime.contract import Part, ToolResultPart
from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.agents.runtime.hooks import GateDecision
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from tests.agents.runtime.hook_fakes import (
    ALLOW,
    APPROVED,
    EchoTool,
    NoPreviewTool,
    ScriptedGate,
    SwitchingWorkspaces,
    ask,
    deny,
    echo,
    run_graph,
    text_reply,
    tool_reply,
)


def _results(parts: list[Part]) -> dict[str, tuple[str, bool]]:
    return {p.tool_call_id: (p.content, p.is_error) for p in parts if isinstance(p, ToolResultPart)}


async def test_a_denied_call_gets_the_denial_and_the_others_run(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    tool = EchoTool()
    gate = ScriptedGate({"c2": deny("Denied at level suggest: write_code.")})
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a"), echo("c2", "b"), echo("c3", "c")), text_reply("ok")],
        tools=(tool,),
        gate=gate,
    )

    assert tool.ran == ["a", "c"]
    assert _results(run.parts) == {
        "c1": ("echo a", False),
        "c2": ("Denied at level suggest: write_code.", True),
        "c3": ("echo c", False),
    }
    assert gate.waited == []
    # The model sees the denial as the call's tool result.
    contents = [str(m.content) for m in run.caller.requests[1]]
    assert "Denied at level suggest: write_code." in contents


async def test_an_ask_reaches_wait_and_runs_once_approved(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    tool = EchoTool()
    gate = ScriptedGate({"c1": ask("apr_1")})
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a"), echo("c2", "b")), text_reply("ok")],
        tools=(tool,),
        gate=gate,
    )

    assert gate.waited == [{"c1": ask("apr_1"), "c2": ALLOW}]
    assert tool.ran == ["a", "b"]
    assert run.state["decisions"] == {"c1": APPROVED, "c2": ALLOW}


async def test_a_rejected_call_gets_its_message_or_a_fixed_text(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    tool = EchoTool()
    rejected = GateDecision(outcome="rejected", message=None, approval_id="apr_1", preview=None)
    noted = GateDecision(
        outcome="rejected",
        message="The user rejected this action: not now.",
        approval_id="apr_2",
        preview=None,
    )
    gate = ScriptedGate({"c1": rejected, "c2": noted})
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a"), echo("c2", "b")), text_reply("ok")],
        tools=(tool,),
        gate=gate,
    )

    assert tool.ran == []
    assert _results(run.parts) == {
        "c1": ("The user rejected this action.", True),
        "c2": ("The user rejected this action: not now.", True),
    }


async def test_invalid_arguments_and_unknown_tools_never_reach_the_gate(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    tool = EchoTool()
    gate = ScriptedGate()
    bad_args: ToolCall = {"id": "c1", "name": "Echo", "args": {}, "type": "tool_call"}
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(bad_args, echo("c2", "x", name="Missing")), text_reply("ok")],
        tools=(tool,),
        gate=gate,
    )

    assert gate.decided == []
    results = _results(run.parts)
    assert results["c1"][1] is True
    assert results["c1"][0].startswith("Invalid arguments for Echo: text:")
    assert results["c2"] == ("There is no tool named Missing.", True)


async def test_a_failing_preview_is_answered_without_a_gate_call(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    echo_tool = EchoTool()
    gate = ScriptedGate()
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a.py", name="NoPreview"), echo("c2", "b")), text_reply("ok")],
        tools=(NoPreviewTool(), echo_tool),
        gate=gate,
    )

    assert [[c.tool_call_id for c in step] for step in gate.decided] == [["c2"]]
    assert _results(run.parts) == {
        "c1": ("There is no file a.py.", True),
        "c2": ("echo b", False),
    }


async def test_gate_calls_carry_the_agent_message_and_the_step_context(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    gate = ScriptedGate()
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a")), text_reply("ok")],
        tools=(EchoTool(),),
        gate=gate,
    )

    (call,) = gate.decided[0]
    assert call.message_id == run.message_id
    assert call.args.model_dump() == {"text": "a"}
    ctx = call.ctx
    assert (ctx.project_id, ctx.agent) == ("prj_1", "orchestrator")
    assert (ctx.session_id, ctx.turn_id, ctx.task) == (run.session_id, run.turn_id, None)


async def test_each_call_gets_its_own_context_and_sees_a_task_change(
    tmp_path: Path, paths: DataPaths, databases: Databases
) -> None:
    repository = RepoFiles(tmp_path / "repo", GitRunner(paths.bin))
    task_files = RepoFiles(tmp_path / "worktree", GitRunner(paths.bin))
    workspaces = SwitchingWorkspaces(repository, task_files)
    task = TaskRef(
        id="tsk_1", title="t", branch="b", worktree=tmp_path / "worktree", status="active"
    )

    def start_task() -> None:
        workspaces.task = task

    tool = EchoTool(after_run=start_task)
    gate = ScriptedGate()
    run = await run_graph(
        tmp_path,
        paths.bin,
        databases,
        [tool_reply(echo("c1", "a"), echo("c2", "b"), echo("c3", "c")), text_reply("ok")],
        tools=(tool,),
        gate=gate,
        workspaces=workspaces,
    )

    # decide judged every call with the context from before any of them ran.
    assert [c.ctx.task for c in gate.decided[0]] == [None, None, None]
    assert [c.task for c in tool.contexts] == [None, task, task]
    last = tool.contexts[-1]
    assert (last.project_id, last.agent) == ("prj_1", "orchestrator")
    assert (last.session_id, last.turn_id) == (run.session_id, run.turn_id)
    # One view per task for the turn: the repository's is the one the builder listed for the
    # layout, and the task's is fetched once, when the task first appears.
    assert [c.files for c in tool.contexts] == [run.agent.files, task_files, task_files]
    assert workspaces.files_calls == [task]


async def test_the_decisions_are_checkpointed_as_gate_decisions(
    tmp_path: Path, paths: DataPaths, databases: Databases, caplog: pytest.LogCaptureFixture
) -> None:
    await databases.project("prj_1")
    conn = await connect_checkpointer(paths.project_db("prj_1"))
    checkpointer = turn_checkpointer(conn)
    await checkpointer.setup()
    approved = replace(ask("apr_1"), outcome="approved")
    turn_id: str | None = None
    try:
        run = await run_graph(
            tmp_path,
            paths.bin,
            databases,
            [tool_reply(echo("c1", "a")), text_reply("ok")],
            tools=(EchoTool(),),
            gate=ScriptedGate({"c1": ask("apr_1")}, approve_as=approved),
            checkpointer=checkpointer,
        )
        turn_id = run.turn_id
        config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
        saved = await checkpointer.aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]  # LangGraph aget_tuple is partially untyped
        assert saved is not None
        assert saved.checkpoint["channel_values"]["decisions"] == {"c1": approved}
        # Read back as the gate's own types, with nothing blocked or merely tolerated.
        assert not [r for r in caplog.records if "msgpack" in r.getMessage()]
    finally:
        if turn_id is not None:
            await checkpointer.adelete_thread(turn_id)
        await conn.close()
