"""One path through the real gate and `handle_turn_waiting`: a tool of class propose asks at
every level, bypass included; its stored part carries the call's proposal kind, and the tool's
context carries the call's ID, the same one its approval stores (I13)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import httpx2
import pytest
from pydantic import BaseModel

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.contract import ApprovalPart, TurnEnded
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import SessionEnv, build_session_env
from tests.agents.runtime.test_resuming_turns import (
    collect_until_turn_ended,
    collect_until_turn_waiting,
    make_tool_reg,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class ProposeArgs(BaseModel):
    kind: str
    payload: dict[str, str] = {}


class RecordingProposeTool(AgentTool[ProposeArgs]):
    name: ClassVar[str] = "propose_test"
    description: ClassVar[str] = "A tool of class propose"
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = ProposeArgs

    def __init__(self) -> None:
        self.preview_call_ids: list[str] = []
        self.run_call_ids: list[str] = []

    async def preview(self, ctx: ToolContext, args: ProposeArgs) -> Preview:
        self.preview_call_ids.append(ctx.tool_call_id)
        return Preview(kind="text", title=f"Propose {args.kind}", body="{}")

    async def run(self, ctx: ToolContext, args: ProposeArgs) -> ToolOutput:
        self.run_call_ids.append(ctx.tool_call_id)
        return ToolOutput("Applied.")


def _definition(tmp_path: Path) -> Path:
    def_dir = tmp_path / "definitions"
    def_dir.mkdir(parents=True, exist_ok=True)
    (def_dir / "proposer.md").write_text(
        "---\nname: proposer\ndisplay_name: Proposer\ndescription: Test agent\n"
        "chat: true\ntools:\n  - propose_test\n---\nPrompt\n",
        encoding="utf-8",
    )
    (def_dir / "writer.md").write_text(
        "---\nname: writer\ndisplay_name: Writer\ndescription: Test agent\n"
        "chat: true\ntools:\n  - write_code_tool\n---\nPrompt\n",
        encoding="utf-8",
    )
    return def_dir


async def _stored_approval_parts(env: SessionEnv, agent: str) -> list[ApprovalPart]:
    """The approval parts as stored, not the hub's live copy of a busy turn's message."""
    session_id = await env.store.session_id(env.project_id, agent)
    return [
        p
        for m in await env.store.messages(env.project_id, session_id)
        for p in m.parts
        if isinstance(p, ApprovalPart)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["suggest", "ask", "autonomous", "bypass"])
async def test_a_propose_call_asks_at_every_level_and_its_part_carries_the_proposal_kind(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    level: PermissionLevel,
) -> None:
    tool = RecordingProposeTool()
    store = ApprovalStore(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[_definition(tmp_path)],
        tools=make_tool_reg(tool),
        gate=ApprovalGate(databases, store=store),
        approval_store=store,
    )
    await set_permission_level(databases, env.project_id, level)
    await env.settings.set_default_model("proposer", f"{env.provider}:fake-tools")
    approvals = ApprovalsService(databases, env.manager, store=store)
    fake_openai.state.scripted_tool_calls = [
        {"name": "propose_test", "arguments": {"kind": "handoff", "payload": {}}}
    ]

    sub = env.manager.subscribe(env.project_id, "proposer", after_seq=None)
    await anext(sub)
    await env.manager.send(env.project_id, "proposer", "Propose the handoff")
    await collect_until_turn_waiting(sub)

    [pending] = await approvals.pending(env.project_id)
    assert (pending.kind, pending.proposal_kind) == ("tool", "handoff")
    [part] = await _stored_approval_parts(env, "proposer")
    assert (part.approval_id, part.kind, part.proposal_kind) == (pending.id, "tool", "handoff")
    assert tool.preview_call_ids
    assert set(tool.preview_call_ids) == {pending.tool_call_id}

    await approvals.decide(env.project_id, pending.id, approve=True)
    ended = [e for e in await collect_until_turn_ended(sub) if isinstance(e, TurnEnded)]
    assert ended[-1].data.status == "done"
    assert tool.run_call_ids == [pending.tool_call_id]


@pytest.mark.asyncio
async def test_a_call_of_another_class_has_no_proposal_kind(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    store = ApprovalStore(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[_definition(tmp_path)],
        tools=make_tool_reg(RecordingProposeTool()),
        gate=ApprovalGate(databases, store=store),
        approval_store=store,
    )
    await set_permission_level(databases, env.project_id, "ask")
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")
    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "x"}}
    ]

    sub = env.manager.subscribe(env.project_id, "writer", after_seq=None)
    await anext(sub)
    await env.manager.send(env.project_id, "writer", "Write a.py")
    await collect_until_turn_waiting(sub)

    [pending] = await ApprovalsService(databases, env.manager, store=store).pending(env.project_id)
    assert (pending.kind, pending.proposal_kind) == ("tool", None)
    [part] = await _stored_approval_parts(env, "writer")
    assert part.proposal_kind is None
    await env.manager.stop(env.project_id, "writer")
