"""A resumed turn that ends before its graph runs again leaves no checkpoint thread behind."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
import httpx2
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.runtime import waiting
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded, TurnWaiting
from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from tests.agents.runtime.session_env import SessionEnv, build_session_env
from tests.agents.runtime.test_resuming_turns import (
    DummyWriteTool,
    collect_until_turn_ended,
    collect_until_turn_waiting,
    make_tool_reg,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "writer"


class ResumeHook:
    """Renders plainly, or, once `block` is set, blocks until the turn is stopped."""

    name = "gate"

    def __init__(self) -> None:
        self.block = False
        self.blocked = asyncio.Event()

    async def render(self, ctx: HookContext) -> str:
        if self.block:
            self.blocked.set()
            await asyncio.Event().wait()
        return "hook text"


class Waiting:
    """A writer turn waiting on its approval, with its subscription."""

    def __init__(
        self, env: SessionEnv, approvals: ApprovalsService, sub: AsyncGenerator[StreamEvent]
    ) -> None:
        self.env = env
        self.approvals = approvals
        self.sub = sub
        self.turn_id = ""

    async def approve(self) -> None:
        (pending,) = await self.approvals.pending(self.env.project_id)
        await self.approvals.decide(self.env.project_id, pending.id, approve=True, note=None)

    async def ended(self) -> TurnEnded:
        events = await collect_until_turn_ended(self.sub)
        return next(e for e in events if isinstance(e, TurnEnded))

    async def thread(self) -> CheckpointTuple | None:
        conn = await connect_checkpointer(self.env.store.db_path(self.env.project_id))
        try:
            saver = turn_checkpointer(conn)
            await saver.setup()
            return await saver.aget_tuple({"configurable": {"thread_id": self.turn_id}})
        finally:
            await conn.close()


async def _waiting_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    hook: ResumeHook,
) -> Waiting:
    folder = tmp_path / "definitions"
    folder.mkdir()
    (folder / f"{AGENT}.md").write_text(
        f"---\nname: {AGENT}\ndisplay_name: Writer\ndescription: Test agent\n"
        "tools:\n  - write_code_tool\ncontext_hooks: [gate]\n---\nPrompt\n",
        encoding="utf-8",
    )
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
        extra_dirs=[folder],
        tools=make_tool_reg(DummyWriteTool()),
        gate=ApprovalGate(databases, store=store),
        approval_store=store,
        hooks=hooks,
    )
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-tools")
    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "print(1)"}}
    ]
    sub = env.manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)
    await env.manager.send(env.project_id, AGENT, "Please write a.py")
    events = await collect_until_turn_waiting(sub)
    turn = Waiting(
        env, ApprovalsService(databases=databases, sessions=env.manager, store=store), sub
    )
    turn.turn_id = next(e for e in events if isinstance(e, TurnWaiting)).data.turn_id
    assert await turn.thread() is not None
    return turn


@pytest.fixture
async def turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    hook: ResumeHook,
) -> AsyncGenerator[Waiting]:
    waiting_turn = await _waiting_turn(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, hook
    )
    yield waiting_turn
    await waiting_turn.env.manager.close()


@pytest.fixture
def hook() -> ResumeHook:
    return ResumeHook()


async def test_a_build_failing_on_resume_deletes_the_thread(turn: Waiting) -> None:
    await turn.env.settings.set_default_model(AGENT, None)

    await turn.approve()

    assert (await turn.ended()).data.status == "failed"
    assert await turn.thread() is None


async def test_a_checkpoint_read_failing_on_resume_deletes_the_thread(
    turn: Waiting, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingRead(AsyncSqliteSaver):
        async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
            raise RuntimeError("checkpoint unreadable")

    def failing_checkpointer(conn: aiosqlite.Connection) -> AsyncSqliteSaver:
        return FailingRead(conn)

    # Patched only now: the first run wrote the checkpoint through the real one.
    monkeypatch.setattr(waiting, "turn_checkpointer", failing_checkpointer)
    await turn.approve()
    ended = await turn.ended()
    monkeypatch.undo()

    assert ended.data.status == "failed"
    assert await turn.thread() is None


async def test_a_stop_during_the_resume_hooks_deletes_the_thread(
    turn: Waiting, hook: ResumeHook
) -> None:
    hook.block = True
    await turn.approve()
    await asyncio.wait_for(hook.blocked.wait(), timeout=10)

    await turn.env.manager.stop(turn.env.project_id, AGENT)

    assert (await turn.ended()).data.status == "stopped"
    assert await turn.thread() is None
