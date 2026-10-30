"""A stalled turn across a restart, the edges of its Try again (a failing call, a deleted
provider), and a re-run of the model node that already stalled: the stall is an interrupted graph
like any waiting turn, and its approval is never duplicated."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.model_caller import ModelKeysExhausted
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.runtime.stall import StallState, record_stall
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.waiting import UNEXPECTED_FAILURE_TEXT
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import KEYLESS
from mlops_agents.providers.failover import KeyFailover, KeyState
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.approval_env import add_turn
from tests.agents.runtime.hook_fakes import NoInbound
from tests.agents.runtime.session_env import collect_until_turn_ended
from tests.agents.runtime.stall_env import (
    KEY,
    StallEnv,
    build_stall_env,
    send_until_waiting,
    stall_parts,
)
from tests.agents.runtime.test_executor_hooks import ChangingHook
from tests.agents.runtime.test_resuming_turns import (
    DummyWriteTool,
    collect_until_turn_waiting,
    make_tool_reg,
)
from tests.agents.runtime.test_stall_prepare import add_long_history
from tests.agents.test_approvals_service import FakeSessionsForApproval
from tests.conftest import STAGE_2_TOOLS, STAGE_3_HOOKS, MemoryKeyStore, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.providers.fake_clock import START, FakeClock, provider


def _restarted(
    env: StallEnv,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    model_http: httpx2.AsyncClient,
    *,
    tools: ToolRegistry | None = None,
    extra_dirs: list[Path] | None = None,
    hooks: HookRegistry | None = None,
) -> tuple[SessionManager, ApprovalsService]:
    """A new app over the same databases: a fresh key failover, sessions and approvals."""
    tools = tools if tools is not None else default_registry()
    catalog = AgentCatalog.load(
        tools.names() | STAGE_2_TOOLS,
        extra_dirs=extra_dirs or (),
        hook_names=(hooks.names() | STAGE_3_HOOKS) if hooks is not None else STAGE_3_HOOKS,
    )
    failover = KeyFailover(FakeClock())
    providers = ProvidersService(databases, keystore, model_http, failover)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    workspace = WorkspaceService(
        load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)}),
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )
    store = ApprovalStore(databases)
    manager = SessionManager(
        databases=databases,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        settings=AgentSettingsService(databases, providers, catalog),
        providers=providers,
        http=model_http,
        failover=failover,
        gate=ApprovalGate(databases, store=store, event_log=env.events),
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=FakeWorkspaces(workspace=workspace, git=GitRunner(paths.bin)),
        approval_store=store,
        event_log=env.events,
        hooks=hooks,
    )
    approvals = ApprovalsService(databases, manager, store=store, event_log=env.events)
    manager.bind_approvals(approvals)
    return manager, approvals


async def test_a_stalled_turn_survives_a_restart_and_try_again_resumes_it(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.script_for_key(KEY, ["401"])
    _, _, turn_id = await send_until_waiting(env, "hi")
    [stall] = await env.stalls(turn_id)
    await env.session.manager.close()

    manager, approvals = _restarted(env, paths, databases, keystore, model_http)
    view = await manager.get(env.project_id, "orchestrator")

    assert view.busy
    [message] = [m for m in view.messages if m.turn_id == turn_id and m.author_kind == "agent"]
    assert message.status == "waiting"
    assert [(p.approval_id, p.status) for p in stall_parts(message)] == [(stall.id, "pending")]
    assert [a.id for a in await approvals.pending(env.project_id)] == [stall.id]

    stream = manager.subscribe(env.project_id, "orchestrator", after_seq=None)
    await anext(stream)
    await approvals.decide(env.project_id, stall.id, approve=True, note=None)
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = next(e for e in events if isinstance(e, TurnEnded))
    assert (ended.data.turn_id, ended.data.status) == (turn_id, "done")
    assert await env.event_payloads("turn.resumed") == [{"turn_id": turn_id}]
    assert await env.checkpoint(turn_id) is None
    await manager.close()


def _hooked_writer(tmp_path: Path) -> Path:
    folder = tmp_path / "hooked"
    folder.mkdir()
    (folder / "writer.md").write_text(
        "---\nname: writer\ndisplay_name: writer\ndescription: Test agent\n"
        "tools:\n  - write_code_tool\ncontext_hooks: [spy]\n---\nPrompt\n",
        encoding="utf-8",
    )
    return folder


async def test_a_turn_stalled_in_prepare_resumes_there_after_a_restart_with_fresh_hooks(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    hook = ChangingHook()
    hooks = HookRegistry()
    hooks.register(hook)
    write_tool = DummyWriteTool()
    tools = make_tool_reg(write_tool)
    extra_dirs = [_hooked_writer(tmp_path)]
    env = await build_stall_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        tools=tools,
        extra_dirs=extra_dirs,
        hooks=hooks,
    )
    await env.session.settings.set_default_model("writer", f"{env.session.provider}:fake-tools")
    await add_long_history(env, "writer")
    fake_openai.state.script_for_key(KEY, ["401"])
    _, _, turn_id = await send_until_waiting(env, "Please write a.py", agent="writer")
    [stall] = await env.stalls(turn_id)
    values = await env.checkpoint(turn_id)
    assert values is not None
    assert isinstance(values["stall"], StallState)
    assert values["stall"].node == "prepare"
    await env.session.manager.close()
    fake_openai.state.requests.clear()

    manager, approvals = _restarted(
        env, paths, databases, keystore, model_http, tools=tools, extra_dirs=extra_dirs, hooks=hooks
    )
    await manager.get(env.project_id, "writer")
    stream = manager.subscribe(env.project_id, "writer", after_seq=None)
    await anext(stream)
    await approvals.decide(env.project_id, stall.id, approve=True, note=None)
    await collect_until_turn_waiting(stream)

    # prepare ran again: compaction, then the model step with the hook text the resume rendered.
    compaction, first_step = fake_openai.state.requests
    assert "Provide a concise summary" in str(compaction["messages"])
    system = str(first_step["messages"][0]["content"])
    assert system.endswith('<data source="hook:spy">\nhook text 2\n</data>')
    # A tool approval after the resolved stall: approving it runs the tool, not prepare again.
    [tool_approval] = [a for a in await approvals.pending(env.project_id) if a.kind == "tool"]
    await approvals.decide(env.project_id, tool_approval.id, approve=True, note=None)
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = next(e for e in events if isinstance(e, TurnEnded))
    assert (ended.data.turn_id, ended.data.status) == (turn_id, "done")
    assert len(write_tool.invoked) == 1
    summaries = [r for r in fake_openai.state.requests if "concise summary" in str(r["messages"])]
    assert len(summaries) == 1
    await manager.close()


async def test_try_again_after_the_provider_was_deleted_fails_the_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.script_for_key(KEY, ["401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [stall] = await env.stalls(turn_id)
    await env.session.providers.delete_provider(env.session.provider)

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = next(e for e in events if isinstance(e, TurnEnded))
    # The resume builds the agent before its graph runs: the deleted provider's model fails it there,
    # with the lookup's own message, not the generic failure text.
    with pytest.raises(DomainError) as lookup:
        await env.session.settings.resolve(env.project_id, "orchestrator")
    # Deleting the provider deleted its models, so the lookup that fails is the model's, not
    # `provider_not_found` (reported on the task).
    assert lookup.value.code == "model_missing"
    assert (ended.data.status, ended.data.error) == ("failed", lookup.value.message)
    assert ended.data.error != UNEXPECTED_FAILURE_TEXT
    assert not [r for r in caplog.records if "failed unexpectedly" in r.message]
    assert await env.event_payloads("turn.resumed") == []
    await env.session.manager.close()


async def test_try_again_clears_the_needs_attention_mark_even_when_the_call_then_fails(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.script_for_key(KEY, ["401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [stall] = await env.stalls(turn_id)
    providers = env.session.providers
    assert (await providers.get_provider(env.session.provider)).status == "needs_attention"
    # The call after Try again fails on an overloaded server, so it clears nothing itself.
    fake_openai.state.server_error_remaining = 3

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = next(e for e in events if isinstance(e, TurnEnded))
    assert ended.data.status == "failed"
    assert (await providers.get_provider(env.session.provider)).status == "ok"
    await env.session.manager.close()


async def test_try_again_after_switching_models_and_deleting_the_provider_continues_the_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # The user repairs the stall: another provider's model, the dead provider deleted. Deleting
    # it cleared its key states, so Try again has nothing to reset and the turn goes on.
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.script_for_key(KEY, ["401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [stall] = await env.stalls(turn_id)
    providers = env.session.providers
    other = await providers.create_provider(
        ProviderCreate(name="other", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers.add_model(ModelCreate(provider=other.name, wire_model_id="fake-tools"))
    await providers.verify_model(f"{other.name}:fake-tools")
    await env.session.settings.set_default_model("orchestrator", f"{other.name}:fake-tools")
    await providers.delete_provider(env.session.provider)
    fake_openai.state.request_headers.clear()

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = next(e for e in events if isinstance(e, TurnEnded))
    assert (ended.data.turn_id, ended.data.status) == (turn_id, "done")
    assert await env.event_payloads("turn.resumed") == [{"turn_id": turn_id}]
    # The model node ran again on the new, keyless provider's model.
    assert fake_openai.state.request_headers
    assert {h["authorization"] for h in fake_openai.state.request_headers} == {f"Bearer {KEYLESS}"}
    await env.session.manager.close()


async def test_a_rerun_of_the_model_node_finds_its_pending_stall(databases: Databases) -> None:
    # A crash between creating the stall and the checkpoint re-runs the model node.
    await add_turn(databases, "prj_rerun", status="running")
    store = ApprovalStore(databases)
    events = SqliteEventLog(databases)
    approvals = ApprovalsService(
        databases, FakeSessionsForApproval(SessionStore(databases)), store=store, event_log=events
    )
    exhausted = ModelKeysExhausted(
        provider=provider(1),
        model="fake-tools",
        keys=[KeyState(key_index=0, status="rejected", since=START)],
        description="Key 1: rejected by the provider.",
    )

    stalls = [
        await record_stall(
            approvals,
            events,
            project_id="prj_rerun",
            agent="orchestrator",
            turn_id="trn_1",
            step=3,
            exhausted=exhausted,
            node="model",
        )
        for _ in range(2)
    ]

    assert stalls[0] == stalls[1]
    records = await store.list_for_step("prj_rerun", "trn_1")
    assert [(r.id, r.kind, r.step) for r in records] == [(stalls[0].approval_id, "stall", 3)]
    # The event is informational: a re-run may record it again (accepted, stated in the PR).
    stalled = await events.history("prj_rerun", types=["turn.stalled"])
    assert [e.payload for e in stalled] == [
        {"turn_id": "trn_1", "provider": "prov_1", "model": "fake-tools"}
    ] * 2
