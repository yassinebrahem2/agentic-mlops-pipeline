"""The hooks through the real SessionManager: at turn start, and again when a waiting turn
resumes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.runtime.contract import TextPart, TurnEnded
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import SessionEnv, build_session_env
from tests.agents.runtime.test_resuming_turns import (
    DummyWriteTool,
    collect_until_turn_ended,
    collect_until_turn_waiting,
    make_tool_reg,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

TRIGGER = "Please write a.py"


class ChangingHook:
    """A spy whose text changes on each render, as data does while a turn waits."""

    name = "spy"

    def __init__(self) -> None:
        self.seen: list[HookContext] = []

    async def render(self, ctx: HookContext) -> str:
        self.seen.append(ctx)
        return f"hook text {len(self.seen)}"


def _definitions(tmp_path: Path) -> Path:
    folder = tmp_path / "definitions"
    folder.mkdir()
    for name, hooks in (("writer", "context_hooks: [spy]\n"), ("plainwriter", "")):
        (folder / f"{name}.md").write_text(
            f"---\nname: {name}\ndisplay_name: {name}\ndescription: Test agent\n"
            f"tools:\n  - write_code_tool\n{hooks}---\nPrompt\n",
            encoding="utf-8",
        )
    return folder


async def _env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    hook: ChangingHook,
) -> tuple[SessionEnv, ApprovalsService]:
    hooks = HookRegistry()
    hooks.register(hook)
    store = ApprovalStore(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[_definitions(tmp_path)],
        tools=make_tool_reg(DummyWriteTool()),
        gate=ApprovalGate(databases, store=store),
        approval_store=store,
        hooks=hooks,
    )
    for agent in ("writer", "plainwriter"):
        await env.settings.set_default_model(agent, f"{env.provider}:fake-tools")
    approvals = ApprovalsService(databases=databases, sessions=env.manager, store=store)
    env.manager.bind_approvals(approvals)
    return env, approvals


async def _wait_then_approve(
    env: SessionEnv, approvals: ApprovalsService, fake_openai: RunningFake, agent: str
) -> list[list[dict[str, Any]]]:
    """Run one turn that waits on an approval, approve it; every model request's messages."""
    fake_openai.state.requests.clear()  # the setup's model verifications
    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "print(1)"}}
    ]
    sub = env.manager.subscribe(env.project_id, agent, after_seq=None)
    await anext(sub)
    await env.manager.send(env.project_id, agent, TRIGGER)
    await collect_until_turn_waiting(sub)
    assert len(fake_openai.state.requests) == 1

    (pending,) = await approvals.pending(env.project_id)
    await approvals.decide(env.project_id, pending.id, approve=True, note=None)
    ended = [e for e in await collect_until_turn_ended(sub) if isinstance(e, TurnEnded)]
    assert ended[0].data.status == "done"
    return [list(r["messages"]) for r in fake_openai.state.requests]


def _system_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m["role"] == "system"]


async def test_a_resumed_turn_runs_its_hooks_again_and_holds_one_system_message(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    hook = ChangingHook()
    env, approvals = await _env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, hook
    )

    first, resumed = await _wait_then_approve(env, approvals, fake_openai, "writer")

    assert [ctx.trigger_text for ctx in hook.seen] == [TRIGGER, TRIGGER]
    assert {ctx.agent for ctx in hook.seen} == {"writer"}
    assert first[0]["role"] == "system"
    assert _system_messages(first) == [str(first[0]["content"])]
    assert str(first[0]["content"]).endswith('<data source="hook:spy">\nhook text 1\n</data>')

    assert resumed[0]["role"] == "system"
    assert len(_system_messages(resumed)) == 1
    resumed_system = str(resumed[0]["content"])
    assert resumed_system.endswith('<data source="hook:spy">\nhook text 2\n</data>')
    assert "hook text 1" not in resumed_system
    # The resumed request continues the same conversation: the trigger, the call, its result.
    assert [m["role"] for m in resumed[1:]] == ["user", "assistant", "tool"]

    session_id = await env.store.session_id(env.project_id, "writer")
    stored = await env.store.messages(env.project_id, session_id)
    stored_texts = [p.text for m in stored for p in m.parts if isinstance(p, TextPart)]
    assert stored_texts
    assert not any("<data" in text or "hook text" in text for text in stored_texts)
    await env.manager.close()


async def test_an_agent_without_hooks_resumes_as_before(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    hook = ChangingHook()
    env, approvals = await _env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, hook
    )

    first, resumed = await _wait_then_approve(env, approvals, fake_openai, "plainwriter")

    assert hook.seen == []
    assert _system_messages(resumed) == _system_messages(first)
    assert resumed[0]["role"] == "system"
    assert '<data source="hook:' not in str(resumed[0]["content"])
    await env.manager.close()
