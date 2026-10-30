"""Tests for ProposalApprovalObserver, superseding and restart recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalKind, ApprovalStatus
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.api import services as api_services
from mlops_agents.api.services import Services
from mlops_agents.credentials.project_secrets import proposal_slot_ref
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    ApplyResult,
    CredentialSlot,
    ProposalView,
)
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.observer import ProposalApprovalObserver
from mlops_agents.proposals.payloads import ConnectionsPayload
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.storage.tables import proposals as proposals_table
from mlops_agents.storage.tables import sessions, turns
from mlops_agents.workspace.service import Project, WorkspaceService
from tests.api.conftest import add_local_project, started_app
from tests.fakes.fake_openai import RunningFake
from tests.fakes.proposal_kinds import swapped_kind
from tests.proposals.samples import connections

# ---------------------------------------------------------------------------
# Test Kind & Test Tool
# ---------------------------------------------------------------------------


class ScriptedTestKind:
    name: str = "connections"
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(
        self,
        name: str = "connections",
        slots: list[CredentialSlot] | None = None,
    ) -> None:
        self.name = name
        self._slots = (
            slots
            if slots is not None
            else [
                CredentialSlot(index=0, label="Key 0", source_type="token"),
                CredentialSlot(index=1, label="Key 1", source_type="password"),
            ]
        )
        self.apply_calls: list[tuple[str, str, Any]] = []
        self.on_apply: Any = None

    def key(self, payload: BaseModel) -> str:
        return "const_key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Test Preview", body="preview text")

    def warnings(self, payload: BaseModel) -> list[str]:
        return ["Warning 1"]

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return list(self._slots)

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        self.apply_calls.append((project_id, proposal_id, payload))
        if self.on_apply is not None:
            return await self.on_apply(project_id, proposal_id, payload)
        return ApplyResult(applied=["conn_applied"], failed={})


class ProposeArgs(BaseModel):
    kind: str
    payload: dict[str, Any]


class TestProposeTool(AgentTool[ProposeArgs]):
    __test__ = False
    name = "Propose"
    description = "Propose a configuration change"
    risk = RiskClass.PROPOSE
    args_model = ProposeArgs

    def __init__(self, proposals: ProposalsService, kinds: ProposalKinds) -> None:
        self._proposals = proposals
        self._kinds = kinds
        self.apply_run_order: list[str] = []

    async def preview(self, ctx: ToolContext, args: ProposeArgs) -> Preview:
        kind_obj = self._kinds.get(args.kind)
        validated = kind_obj.payload_model.model_validate(args.payload)
        key = kind_obj.key(validated)
        await self._proposals.create(
            ctx.project_id,
            ctx.agent,
            args.kind,
            key,
            args.payload,
            ctx.turn_id,
            ctx.tool_call_id,
        )
        return Preview(kind="text", title=f"Proposal: {args.kind}", body="test preview")

    async def run(self, ctx: ToolContext, args: ProposeArgs) -> ToolOutput:
        self.apply_run_order.append("tool_run")
        p = await self._proposals.for_call(ctx.project_id, ctx.turn_id, ctx.tool_call_id)
        if p and p.result:
            return ToolOutput(content=f"Applied: {','.join(p.result.applied)}")
        return ToolOutput(content="No result", is_error=True)


def _make_approval_view(
    *,
    id: str = "apr_1",
    agent: str = "orchestrator",
    session_id: str = "ses_1",
    turn_id: str = "trn_1",
    kind: ApprovalKind = "tool",
    tool_call_id: str | None = "call_1",
    tool: str | None = "Propose",
    risk_class: RiskClass | None = RiskClass.PROPOSE,
    step: int | None = 1,
    proposal_kind: str | None = "connections",
    status: ApprovalStatus = "pending",
) -> ApprovalView:
    return ApprovalView(
        id=id,
        agent=agent,
        session_id=session_id,
        turn_id=turn_id,
        kind=kind,
        tool_call_id=tool_call_id,
        tool=tool,
        risk_class=risk_class,
        step=step,
        proposal_kind=proposal_kind,
        preview=Preview(kind="text", title="Test", body="body"),
        status=status,
        note=None,
        created_at="2026-10-28T12:00:00Z",
        decided_at=None,
    )


# ---------------------------------------------------------------------------
# Unit tests for ProposalApprovalObserver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observer_ignores_non_propose_approvals() -> None:
    proposals = MagicMock(spec=ProposalsService)
    approvals = AsyncMock()
    workspace = MagicMock(spec=WorkspaceService)

    observer = ProposalApprovalObserver(proposals, approvals, workspace)

    view = _make_approval_view(proposal_kind=None)
    await observer.asked(view)
    await observer.decided(view)

    proposals.for_call.assert_not_called()
    proposals.get_by_approval.assert_not_called()
    approvals.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_observer_ignores_stalls() -> None:
    proposals = MagicMock(spec=ProposalsService)
    approvals = AsyncMock()
    workspace = MagicMock(spec=WorkspaceService)

    observer = ProposalApprovalObserver(proposals, approvals, workspace)

    view = _make_approval_view(tool_call_id=None, proposal_kind="connections")
    await observer.asked(view)

    proposals.for_call.assert_not_called()
    proposals.attach.assert_not_called()


@pytest.mark.asyncio
async def test_observer_asked_attaches_and_cancels_superseded() -> None:
    proposals = MagicMock(spec=ProposalsService)
    approvals = AsyncMock()
    workspace = MagicMock(spec=WorkspaceService)

    fake_project = MagicMock(spec=Project)
    fake_project.id = "prj_1"
    workspace.list_projects = AsyncMock(return_value=[fake_project])

    fake_draft = MagicMock(spec=ProposalView)
    fake_draft.id = "prp_new"
    proposals.for_call = AsyncMock(return_value=fake_draft)
    proposals.attach = AsyncMock(return_value="apr_old")

    observer = ProposalApprovalObserver(proposals, approvals, workspace)

    view = _make_approval_view(id="apr_new", turn_id="trn_1", tool_call_id="call_1")
    await observer.asked(view)

    proposals.for_call.assert_awaited_once_with("prj_1", "trn_1", "call_1")
    proposals.attach.assert_awaited_once_with("prj_1", "prp_new", "apr_new")
    approvals.cancel.assert_awaited_once_with("prj_1", "apr_old")


@pytest.mark.asyncio
async def test_observer_decided_calls_decide() -> None:
    proposals = MagicMock(spec=ProposalsService)
    approvals = AsyncMock()
    workspace = MagicMock(spec=WorkspaceService)

    fake_project = MagicMock(spec=Project)
    fake_project.id = "prj_1"
    workspace.list_projects = AsyncMock(return_value=[fake_project])

    fake_proposal = MagicMock(spec=ProposalView)
    fake_proposal.id = "prp_1"
    proposals.get_by_approval = AsyncMock(return_value=fake_proposal)
    proposals.decide = AsyncMock(return_value=None)

    observer = ProposalApprovalObserver(proposals, approvals, workspace)

    view = _make_approval_view(id="apr_1", status="approved")
    await observer.decided(view)

    proposals.decide.assert_awaited_once_with("prj_1", "prp_1", approved=True)


@pytest.mark.asyncio
async def test_observer_decided_cancelled_and_expired_reject() -> None:
    proposals = MagicMock(spec=ProposalsService)
    approvals = AsyncMock()
    workspace = MagicMock(spec=WorkspaceService)

    fake_project = MagicMock(spec=Project)
    fake_project.id = "prj_1"
    workspace.list_projects = AsyncMock(return_value=[fake_project])

    fake_proposal = MagicMock(spec=ProposalView)
    fake_proposal.id = "prp_1"
    proposals.get_by_approval = AsyncMock(return_value=fake_proposal)
    proposals.decide = AsyncMock(return_value=None)

    observer = ProposalApprovalObserver(proposals, approvals, workspace)

    # Cancelled
    view_cancelled = _make_approval_view(id="apr_1", status="cancelled")
    await observer.decided(view_cancelled)
    proposals.decide.assert_awaited_with("prj_1", "prp_1", approved=False)

    # Expired
    view_expired = _make_approval_view(id="apr_1", status="expired")
    await observer.decided(view_expired)
    proposals.decide.assert_awaited_with("prj_1", "prp_1", approved=False)


# ---------------------------------------------------------------------------
# Integration tests through real build_services
# ---------------------------------------------------------------------------


async def _use_model(services: Services, fake: RunningFake, wire_model_id: str) -> None:
    provider = await services.providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await services.providers.add_model(
        ModelCreate(provider=provider.name, wire_model_id=wire_model_id)
    )
    await services.providers.verify_model(f"fake:{wire_model_id}")
    await services.agent_settings.set_default_model("orchestrator", f"fake:{wire_model_id}")
    await services.agent_settings.set_default_model("onboarding", f"fake:{wire_model_id}")


async def _pending(services: Services, project_id: str, count: int = 1) -> list[str]:
    async with asyncio.timeout(15):
        while True:
            pending = await services.approvals.pending(project_id)
            if len(pending) >= count:
                return [a.id for a in pending]
            await asyncio.sleep(0.05)


async def _waiting(services: Services, project_id: str, agent: str = "orchestrator") -> None:
    async with asyncio.timeout(15):
        while True:
            if (await services.sessions.turn_states(project_id)).get(agent) == "waiting":
                return
            await asyncio.sleep(0.05)


async def _turn_status(services: Services, project_id: str, turn_id: str) -> str:
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        status = (
            await conn.execute(select(turns.c.status).where(turns.c.id == turn_id))
        ).scalar_one()
    return str(status)


async def _wait_turn_done(services: Services, project_id: str, turn_id: str) -> None:
    async with asyncio.timeout(15):
        while True:
            if await _turn_status(services, project_id, turn_id) == "done":
                return
            await asyncio.sleep(0.05)


@contextmanager
def _propose_env(
    services: Services,
) -> Generator[tuple[ScriptedTestKind, TestProposeTool]]:
    test_kind = ScriptedTestKind(CONNECTIONS_KIND)
    with swapped_kind(services.proposal_kinds, test_kind):
        yield _register_propose_tool(services, test_kind)


def _register_propose_tool(
    services: Services, test_kind: ScriptedTestKind
) -> tuple[ScriptedTestKind, TestProposeTool]:
    tool = TestProposeTool(services.proposals, services.proposal_kinds)
    services.tools.register(tool)

    orch = services.catalog.get("orchestrator")
    services.catalog._by_name["orchestrator"] = orch.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"tools": (*orch.tools, "Propose")}
    )
    onb = services.catalog.get("onboarding")
    services.catalog._by_name["onboarding"] = onb.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"tools": (*onb.tools, "Propose")}
    )
    return test_kind, tool


@pytest.mark.asyncio
async def test_gate_created_approval_fires_asked_and_attaches(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        with _propose_env(services) as (_test_kind, _tool):
            fake_openai.state.scripted_tool_calls = [
                {
                    "name": "Propose",
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]
            await services.sessions.send(project_id, "orchestrator", "Propose connection")
            [approval_id] = await _pending(services, project_id, 1)
            await _waiting(services, project_id)

            proposal = await services.proposals.get_by_approval(project_id, approval_id)
            assert proposal is not None
            assert proposal.approval_id == approval_id
            assert proposal.status == "pending"
            assert proposal.kind == CONNECTIONS_KIND


@pytest.mark.asyncio
async def test_stop_and_recovery_expiry_fire_decided_and_empty_slots(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        with _propose_env(services) as (_test_kind, _tool):
            # 1. Test Stop cancels approval and rejects proposal + empties slot
            fake_openai.state.scripted_tool_calls = [
                {
                    "name": "Propose",
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]
            await services.sessions.send(project_id, "orchestrator", "Propose connection 1")
            [approval_id] = await _pending(services, project_id, 1)
            await _waiting(services, project_id)

            p = await services.proposals.get_by_approval(project_id, approval_id)
            assert p is not None
            await services.proposals.put_credential(project_id, p.id, 0, "secret-token")
            ref0 = proposal_slot_ref(project_id, p.id, 0)
            assert await services.project_secrets.is_set(ref0) is True

            await services.sessions.stop(project_id, "orchestrator")

            p_after = await services.proposals.get_by_approval(project_id, approval_id)
            assert p_after is not None
            assert p_after.status == "rejected"
            assert await services.project_secrets.is_set(ref0) is False

            # 2. Test Recovery expiry rejects proposal + empties slot
            engine = await services.databases.project(project_id)
            async with engine.begin() as conn:
                ses_id = (
                    await conn.execute(
                        select(sessions.c.id).where(sessions.c.agent == "orchestrator")
                    )
                ).scalar_one()
                await conn.execute(
                    turns.insert().values(
                        id="trn_crashed",
                        session_id=ses_id,
                        trigger_message_id="msg_crashed",
                        status="interrupted",
                        error=None,
                        created_at="2026-10-28T10:00:00Z",
                        started_at="2026-10-28T10:00:00Z",
                        ended_at="2026-10-28T10:01:00Z",
                    )
                )
            stale_draft = await services.proposals.create(
                project_id,
                "orchestrator",
                CONNECTIONS_KIND,
                "stale_key",
                connections(),
                "trn_crashed",
                "call_crashed",
            )
            stale_appr = await ApprovalStore(services.databases).create(
                project_id,
                turn_id="trn_crashed",
                session_id=ses_id,
                message_id="msg_crashed",
                agent="orchestrator",
                tool_call_id="call_crashed",
                tool="Propose",
                risk_class=RiskClass.PROPOSE,
                proposal_kind=CONNECTIONS_KIND,
                preview=Preview(kind="text", title="Stale", body="stale"),
            )
            await services.proposals.attach(project_id, stale_draft.id, stale_appr.id)
            await services.proposals.put_credential(project_id, stale_draft.id, 0, "stale-secret")
            ref_stale = proposal_slot_ref(project_id, stale_draft.id, 0)
            assert await services.project_secrets.is_set(ref_stale) is True

            # Trigger recovery (discard from recovered set to simulate restart/fresh check)
            assert isinstance(services.sessions, SessionManager)
            services.sessions._recovery.forget(project_id)  # pyright: ignore[reportPrivateUsage]
            assert await services.approvals.pending(project_id) == []

            p_stale_after = await services.proposals.get_by_approval(project_id, stale_appr.id)
            assert p_stale_after is not None
            assert p_stale_after.status == "rejected"
            assert await services.project_secrets.is_set(ref_stale) is False


@pytest.mark.asyncio
async def test_propose_asks_at_all_permission_levels(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        with _propose_env(services) as (_test_kind, _tool):
            for level in ("suggest", "ask", "autonomous", "bypass"):
                await set_permission_level(services.databases, project_id, level)
                fake_openai.state.scripted_tool_calls = [
                    {
                        "name": "Propose",
                        "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                    }
                ]
                await services.sessions.send(project_id, "orchestrator", f"Propose at {level}")
                [approval_id] = await _pending(services, project_id, 1)
                await _waiting(services, project_id)

                p = await services.proposals.get_by_approval(project_id, approval_id)
                assert p is not None
                assert p.status == "pending"

                # Reject to clean up before next level
                fake_openai.state.scripted_tool_calls = None
                await services.approvals.decide(project_id, approval_id, approve=False, note=None)
                await _wait_turn_done(services, project_id, p.turn_id)


@pytest.mark.asyncio
async def test_approve_applies_before_tool_result_returns(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        with _propose_env(services) as (test_kind, tool):
            tool.apply_run_order.clear()

            async def _record_apply(
                project_id: str, proposal_id: str, payload: BaseModel
            ) -> ApplyResult:
                tool.apply_run_order.append("apply")
                return ApplyResult(applied=["conn_applied"], failed={})

            test_kind.on_apply = _record_apply

            fake_openai.state.scripted_tool_calls = [
                {
                    "name": "Propose",
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]
            await services.sessions.send(
                project_id, "orchestrator", "Propose connection to approve"
            )
            [approval_id] = await _pending(services, project_id, 1)
            await _waiting(services, project_id)

            p_pending = await services.proposals.get_by_approval(project_id, approval_id)
            assert p_pending is not None
            turn_id = p_pending.turn_id

            # Clear scripted calls so resumed turn finishes
            fake_openai.state.scripted_tool_calls = None

            await services.approvals.decide(project_id, approval_id, approve=True, note=None)
            await _wait_turn_done(services, project_id, turn_id)

            # Observers awaited before resume -> apply was called before tool_run
            assert tool.apply_run_order == ["apply", "tool_run"]

            p = await services.proposals.get_by_approval(project_id, approval_id)
            assert p is not None
            assert p.status == "approved"
            assert p.result == ApplyResult(applied=["conn_applied"], failed={})


@pytest.mark.asyncio
async def test_reject_empties_slots(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        with _propose_env(services) as (_test_kind, _tool):
            fake_openai.state.scripted_tool_calls = [
                {
                    "name": "Propose",
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]
            await services.sessions.send(project_id, "orchestrator", "Propose connection to reject")
            [approval_id] = await _pending(services, project_id, 1)
            await _waiting(services, project_id)

            p = await services.proposals.get_by_approval(project_id, approval_id)
            assert p is not None
            await services.proposals.put_credential(project_id, p.id, 0, "secret-value")
            ref = proposal_slot_ref(project_id, p.id, 0)
            assert await services.project_secrets.is_set(ref) is True

            fake_openai.state.scripted_tool_calls = None
            await services.approvals.decide(project_id, approval_id, approve=False, note=None)
            await _wait_turn_done(services, project_id, p.turn_id)

            p_after = await services.proposals.get_by_approval(project_id, approval_id)
            assert p_after is not None
            assert p_after.status == "rejected"
            assert await services.project_secrets.is_set(ref) is False


@pytest.mark.asyncio
async def test_newer_proposal_cancels_older_approval_and_resumes_its_turn(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")
        with _propose_env(services) as (_test_kind, _tool):
            # Session 1 (orchestrator) proposes connections
            fake_openai.state.scripted_tool_calls = [
                {
                    "name": "Propose",
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]
            await services.sessions.send(project_id, "orchestrator", "First proposal")
            [first_approval_id] = await _pending(services, project_id, 1)
            await _waiting(services, project_id, "orchestrator")

            first_p = await services.proposals.get_by_approval(project_id, first_approval_id)
            assert first_p is not None
            assert first_p.status == "pending"

            # Session 2 (onboarding) proposes same kind (same key)
            fake_openai.state.scripted_tool_calls = [
                {
                    "name": "Propose",
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]
            async with asyncio.timeout(10):
                await services.sessions.send(project_id, "onboarding", "Second proposal")
                # The older approval is cancelled, its turn resumes!
                await _wait_turn_done(services, project_id, first_p.turn_id)

                # Orchestrator's approval is cancelled
                appr1 = await ApprovalStore(services.databases).get(project_id, first_approval_id)
                assert appr1 is not None
                assert appr1.status == "cancelled"

                # Older proposal is superseded
                first_p_after = await services.proposals.get_by_approval(
                    project_id, first_approval_id
                )
                assert first_p_after is not None
                assert first_p_after.status == "superseded"

                # Newer approval is pending
                pending_apprs = await services.approvals.pending(project_id)
                assert len(pending_apprs) == 1
                second_approval_id = pending_apprs[0].id
                assert second_approval_id != first_approval_id

                second_p = await services.proposals.get_by_approval(project_id, second_approval_id)
                assert second_p is not None
                assert second_p.status == "pending"
                assert second_p.approval_id == second_approval_id


@pytest.mark.asyncio
async def test_non_propose_approval_never_reaches_proposals_service(
    tmp_path: Path, git_env: dict[str, str], fake_openai: RunningFake
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-write")

        # fake-write calls StartTask (risk class is WRITE_CODE)
        await services.sessions.send(project_id, "orchestrator", "Start task")
        [approval_id] = await _pending(services, project_id, 1)
        await _waiting(services, project_id)

        # It has proposal_kind=None
        p = await services.proposals.get_by_approval(project_id, approval_id)
        assert p is None

        # Even after decide, proposals has no records
        appr = await ApprovalStore(services.databases).get(project_id, approval_id)
        assert appr is not None
        fake_openai.state.scripted_tool_calls = None
        await services.approvals.decide(project_id, approval_id, approve=False, note=None)
        await _wait_turn_done(services, project_id, appr.turn_id)

        engine = await services.databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(proposals_table))).all()
        assert len(rows) == 0


@pytest.mark.asyncio
async def test_restart_with_approved_proposal_without_result_applies_once(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"

    test_kind = ScriptedTestKind(CONNECTIONS_KIND)

    def scripted_connections_kind(*args: Any, **kwargs: Any) -> ScriptedTestKind:
        return test_kind

    # Recovery runs inside build_services, before a test could swap the kind: build_services
    # registers the scripted kind as its connections kind instead.
    monkeypatch.setattr(api_services, "ConnectionsKind", scripted_connections_kind)

    # 1. Start app, create project and approved proposal without result
    async with started_app(data_dir) as app1:
        services1: Services = app1.state.services
        project_id = await add_local_project(app1, tmp_path, git_env)

        draft = await services1.proposals.create(
            project_id,
            "orchestrator",
            CONNECTIONS_KIND,
            "key1",
            connections(),
            "trn_1",
            "call_1",
        )
        # Manually mark it approved with result=None (simulating crash before apply/result)
        engine = await services1.databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                proposals_table.update()
                .where(proposals_table.c.id == draft.id)
                .values(status="approved", result=None)
            )

    # 2. Restart app: recovery runs in build_services for every project
    async with started_app(data_dir) as app2:
        services2: Services = app2.state.services

        # Proposal in database is now applied
        engine2 = await services2.databases.project(project_id)
        async with engine2.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(proposals_table).where(proposals_table.c.id == draft.id)
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "approved"
        assert row["result"] is not None
        assert "conn_applied" in row["result"]
