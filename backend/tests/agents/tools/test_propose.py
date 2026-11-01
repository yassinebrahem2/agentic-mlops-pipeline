"""Tests for the Propose tool (Task 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.hooks import GateCall
from mlops_agents.agents.tools.base import (
    Preview,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.agents.tools.propose import (
    PROPOSE_TOOL_NAME,
    ProposeArgs,
    ProposeTool,
)
from mlops_agents.api.services import Services
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.parser_kind import CODE_PARSER_CONTRACT
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    HANDOFF_KIND,
    ApplyResult,
    CredentialSlot,
    PromotionPayload,
)
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import (
    approvals as approvals_table,
)
from mlops_agents.storage.tables import (
    audit as audit_table,
)
from mlops_agents.storage.tables import (
    events as events_table,
)
from mlops_agents.storage.tables import (
    proposals as proposals_table,
)
from tests.agents.gate_helpers import setup_session_and_turn
from tests.agents.runtime.test_resuming_turns import (
    collect_until_turn_ended,
    collect_until_turn_waiting,
)
from tests.api.conftest import add_local_project, started_app
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.fakes.proposal_kinds import swapped_kind
from tests.proposals.conftest import ScriptedTestKind
from tests.proposals.samples import connections, handoff


class MockFileAccess:
    @property
    def root(self) -> Path:
        return Path()

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""

    def invalidate(self) -> None:
        pass


def _dummy_ctx(
    project_id: str = "prj_test",
    turn_id: str = "trn_1",
    tool_call_id: str = "call_1",
    agent: str = "onboarding",
) -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id=turn_id,
        files=MockFileAccess(),
        task=None,
        tool_call_id=tool_call_id,
    )


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


async def _pending_approvals(services: Services, project_id: str, count: int = 1) -> list[str]:
    async with asyncio.timeout(15):
        while True:
            pending = await services.approvals.pending(project_id)
            if len(pending) >= count:
                return [a.id for a in pending]
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Unit tests on ProposeArgs and ProposeTool parsing
# ---------------------------------------------------------------------------


def test_propose_args_payload_annotation_is_any() -> None:
    assert ProposeArgs.model_fields["payload"].annotation is Any


def test_valid_connections_and_handoff_payloads_parse() -> None:
    conn_args = ProposeArgs.model_validate({"kind": CONNECTIONS_KIND, "payload": connections()})
    assert conn_args.kind == CONNECTIONS_KIND
    assert conn_args.payload.model_dump() == connections()

    hnd_args = ProposeArgs.model_validate({"kind": HANDOFF_KIND, "payload": handoff()})
    assert hnd_args.kind == HANDOFF_KIND
    assert hnd_args.payload.model_dump() == handoff()


def test_unknown_field_in_top_level_args_is_parse_error() -> None:
    proposals_service = MagicMock(spec=ProposalsService)
    kinds = ProposalKinds()
    tool = ProposeTool(proposals_service, kinds)

    out = tool.parse(
        {
            "kind": CONNECTIONS_KIND,
            "payload": connections(),
            "extra_field": "disallowed",
        }
    )
    assert isinstance(out, ToolOutput)
    assert out.is_error
    assert "extra_field" in out.content


@pytest.mark.asyncio
async def test_propose_with_unregistered_kind_is_tool_error_before_gate() -> None:
    proposals_service = MagicMock(spec=ProposalsService)
    kinds = ProposalKinds()
    tool = ProposeTool(proposals_service, kinds)
    ctx = _dummy_ctx()
    args = ProposeArgs(kind="unregistered_kind", payload={})

    with pytest.raises(ToolFailure, match="Unknown proposal kind: unregistered_kind"):
        await tool.preview(ctx, args)
    proposals_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_registered_kind_payload_validated_by_its_model() -> None:
    proposals_service = MagicMock(spec=ProposalsService)
    kinds = ProposalKinds()
    kind = ScriptedTestKind(name=CONNECTIONS_KIND)
    kinds.register(kind)
    tool = ProposeTool(proposals_service, kinds)
    ctx = _dummy_ctx()

    # Invalid payload for connections: missing connections list
    bad_payload = {"invalid_key": "bad"}
    args = ProposeArgs(kind=CONNECTIONS_KIND, payload=bad_payload)
    with pytest.raises(ToolFailure, match="Invalid payload for connections"):
        await tool.preview(ctx, args)
    proposals_service.create.assert_not_called()

    # Extra property in connections list (extra="forbid")
    bad_conn = dict(connections())
    bad_conn["connections"] = [dict(bad_conn["connections"][0], extra_prop="disallowed")]
    args_extra = ProposeArgs(kind=CONNECTIONS_KIND, payload=bad_conn)
    with pytest.raises(ToolFailure, match="extra_prop"):
        await tool.preview(ctx, args_extra)
    proposals_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_fake_kind_with_proposal_check_refused_before_gate_with_lines() -> None:
    class CheckingKind:
        name = "checking_kind"
        payload_model = ConnectionsPayload

        def key(self, payload: Any) -> str:
            return "check_key"

        def preview(self, payload: Any) -> Preview:
            return Preview(kind="text", title="Title", body="body")

        def warnings(self, payload: Any) -> list[str]:
            return []

        def credentials_needed(self, payload: Any) -> list[CredentialSlot]:
            return []

        async def apply(self, project_id: str, proposal_id: str, payload: Any) -> ApplyResult:
            return ApplyResult(applied=[], failed={})

        async def check(self, project_id: str, payload: Any) -> list[str]:
            return ["Refusal 1: table not found", "Refusal 2: permissions lacking"]

    proposals_service = MagicMock(spec=ProposalsService)
    kinds = ProposalKinds()
    kinds.register(CheckingKind())  # pyright: ignore[reportArgumentType]
    tool = ProposeTool(proposals_service, kinds)
    ctx = _dummy_ctx()

    args = ProposeArgs(kind="checking_kind", payload=connections())
    with pytest.raises(ToolFailure) as exc_info:
        await tool.preview(ctx, args)
    assert "Refusal 1: table not found" in str(exc_info.value)
    assert "Refusal 2: permissions lacking" in str(exc_info.value)
    proposals_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_payload_with_bypass_grant_refused_before_any_record_or_approval() -> None:
    proposals_service = MagicMock(spec=ProposalsService)
    kinds = ProposalKinds()
    kinds.register(ScriptedTestKind(name=HANDOFF_KIND, payload_model=HandoffPayload))
    tool = ProposeTool(proposals_service, kinds)

    bad_handoff = dict(handoff())
    bad_handoff["grants"] = {"network": False, "permission_level": "bypass"}

    ctx = _dummy_ctx()
    args = ProposeArgs(kind=HANDOFF_KIND, payload=bad_handoff)
    with pytest.raises(ToolFailure, match="permission_level"):
        await tool.preview(ctx, args)
    proposals_service.create.assert_not_called()


@pytest.mark.asyncio
async def test_fake_promotion_kind_at_bypass_applied_no_approval_and_run_returns_applied_result(
    tmp_path: Path,
) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    project_id = "prj_promo"
    engine = await databases.project(project_id)
    async with engine.begin() as _conn:
        pass

    event_log = SqliteEventLog(databases)
    key_store = MemoryKeyStore()
    project_secrets = ProjectSecrets(key_store, databases)
    kinds = ProposalKinds()
    promo_kind = ScriptedTestKind(name="promotion", payload_model=PromotionPayload)
    kinds.register(promo_kind)
    proposals_service = ProposalsService(databases, project_secrets, kinds, event_log)
    tool = ProposeTool(proposals_service, kinds)

    approval_store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=approval_store, event_log=event_log)

    promo_payload = {
        "model": "mdl_1",
        "report_id": "rep_1",
        "candidate_version": "v2",
        "champion_version": "v1",
        "deploy_hook_id": "hok_1",
    }
    args = ProposeArgs(kind="promotion", payload=promo_payload)

    # 1. At bypass: allowed without approval, Propose.run applies proposal
    await set_permission_level(databases, project_id, "bypass")
    ctx_bypass = _dummy_ctx(project_id=project_id, turn_id="trn_bp", tool_call_id="call_bp")
    preview_bypass = await tool.preview(ctx_bypass, args)
    assert preview_bypass.title == "Proposal: promotion"

    gate_call_bypass = GateCall(
        tool_call_id="call_bp",
        message_id="msg_1",
        tool=tool,
        args=args,
        ctx=ctx_bypass,
    )
    decisions_bypass = await gate.decide([gate_call_bypass])
    dec_bypass = decisions_bypass["call_bp"]
    assert dec_bypass.outcome == "allow"
    assert dec_bypass.approval_id is None

    out_bypass = await tool.run(ctx_bypass, args)
    assert out_bypass.is_error is False
    assert out_bypass.content == "Approved. Everything was applied."
    assert len(promo_kind.apply_calls) == 1

    stored_bp = await proposals_service.for_call(project_id, "trn_bp", "call_bp")
    assert stored_bp is not None
    assert stored_bp.status == "approved"
    assert stored_bp.result is not None

    # 2. At autonomous: gate asks for human approval
    await set_permission_level(databases, project_id, "autonomous")
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_auto")
    ctx_auto = _dummy_ctx(project_id=project_id, turn_id="trn_auto", tool_call_id="call_auto")
    preview_auto = await tool.preview(ctx_auto, args)
    assert preview_auto.title == "Proposal: promotion"

    gate_call_auto = GateCall(
        tool_call_id="call_auto",
        message_id="msg_2",
        tool=tool,
        args=args,
        ctx=ctx_auto,
    )
    decisions_auto = await gate.decide([gate_call_auto])
    dec_auto = decisions_auto["call_auto"]
    assert dec_auto.outcome == "ask"
    assert dec_auto.approval_id is not None

    stored_auto = await proposals_service.for_call(project_id, "trn_auto", "call_auto")
    assert stored_auto is not None
    assert stored_auto.status == "pending"
    assert stored_auto.result is None


# ---------------------------------------------------------------------------
# Previews, idempotency, and duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_previews_of_one_call_leave_one_draft_and_one_event(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)

        test_kind = ScriptedTestKind(CONNECTIONS_KIND)
        with swapped_kind(services.proposal_kinds, test_kind):
            tool = ProposeTool(services.proposals, services.proposal_kinds)

            ctx = _dummy_ctx(project_id, turn_id="trn_preview", tool_call_id="call_preview")
            args = ProposeArgs.model_validate({"kind": CONNECTIONS_KIND, "payload": connections()})

            # First preview
            p1 = await tool.preview(ctx, args)
            assert p1.title == f"Proposal: {CONNECTIONS_KIND}"
            assert p1.kind == "text"

            # Second preview (same ctx with same tool_call_id)
            p2 = await tool.preview(ctx, args)
            assert p2.title == f"Proposal: {CONNECTIONS_KIND}"
            assert p2.body == p1.body

            # Verify exactly 1 draft in db
            engine = await services.databases.project(project_id)
            async with engine.connect() as conn:
                proposals = (
                    await conn.execute(
                        select(proposals_table).where(
                            proposals_table.c.turn_id == "trn_preview",
                            proposals_table.c.tool_call_id == "call_preview",
                        )
                    )
                ).all()
                assert len(proposals) == 1

                # Verify exactly 1 proposal.created event
                events = (
                    await conn.execute(
                        select(events_table).where(events_table.c.type == "proposal.created")
                    )
                ).all()
                assert len(events) == 1


@pytest.mark.asyncio
async def test_two_same_kind_calls_in_one_step_duplicate_tool_error(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)

        test_kind = ScriptedTestKind(CONNECTIONS_KIND)
        with swapped_kind(services.proposal_kinds, test_kind):
            tool = ProposeTool(services.proposals, services.proposal_kinds)

            ctx1 = _dummy_ctx(project_id, turn_id="trn_dup", tool_call_id="call_1")
            ctx2 = _dummy_ctx(project_id, turn_id="trn_dup", tool_call_id="call_2")
            args = ProposeArgs.model_validate({"kind": CONNECTIONS_KIND, "payload": connections()})

            # First call preview succeeds
            await tool.preview(ctx1, args)

            # Second call in the same step with the same kind and key raises ToolFailure
            with pytest.raises(ToolFailure) as exc_info:
                await tool.preview(ctx2, args)

            assert (
                "already exists" in str(exc_info.value).lower()
                or "duplicate" in str(exc_info.value).lower()
            )

            # Call 2 has no draft
            engine = await services.databases.project(project_id)
            async with engine.connect() as conn:
                c2_proposals = (
                    await conn.execute(
                        select(proposals_table).where(proposals_table.c.tool_call_id == "call_2")
                    )
                ).all()
                assert len(c2_proposals) == 0

            # Now simulate approval of call 1 and run
            draft = await services.proposals.for_call(project_id, "trn_dup", "call_1")
            assert draft is not None
            await services.proposals.decide(project_id, draft.id, approved=True)

            out = await tool.run(ctx1, args)
            assert not out.is_error
            assert "Approved" in out.content


# ---------------------------------------------------------------------------
# Graph integration with fake model: permission levels, approve, reject, failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["suggest", "ask", "autonomous", "bypass"])
async def test_propose_pauses_at_every_level_including_bypass(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
    level: PermissionLevel,
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")

        test_kind = ScriptedTestKind(CONNECTIONS_KIND)
        with swapped_kind(services.proposal_kinds, test_kind):
            # Ensure onboarding has Propose tool
            onb = services.catalog.get("onboarding")
            services.catalog._by_name["onboarding"] = onb.model_copy(  # pyright: ignore[reportPrivateUsage]
                update={"tools": (*onb.tools, PROPOSE_TOOL_NAME)}
            )

            await set_permission_level(services.databases, project_id, level)

            fake_openai.state.scripted_tool_calls = [
                {
                    "name": PROPOSE_TOOL_NAME,
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]

            sub = services.sessions.subscribe(project_id, "onboarding", after_seq=None)
            await anext(sub)
            await services.sessions.send(project_id, "onboarding", "Propose connections")

            # The turn must pause and wait for approval at EVERY permission level
            await collect_until_turn_waiting(sub)

            [approval_id] = await _pending_approvals(services, project_id, 1)
            pending = await ApprovalStore(services.databases).get(project_id, approval_id)
            assert pending is not None
            assert pending.proposal_kind == CONNECTIONS_KIND
            assert pending.tool == PROPOSE_TOOL_NAME

            # Approve resumes turn and succeeds
            fake_openai.state.scripted_tool_calls = None
            await services.approvals.decide(project_id, approval_id, approve=True, note=None)

            events = await collect_until_turn_ended(sub)
            ended = [e for e in events if isinstance(e, TurnEnded)]
            assert ended[-1].data.status == "done"


@pytest.mark.asyncio
async def test_propose_reject_resumes_with_rejection(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")

        test_kind = ScriptedTestKind(CONNECTIONS_KIND)
        with swapped_kind(services.proposal_kinds, test_kind):
            onb = services.catalog.get("onboarding")
            services.catalog._by_name["onboarding"] = onb.model_copy(  # pyright: ignore[reportPrivateUsage]
                update={"tools": (*onb.tools, PROPOSE_TOOL_NAME)}
            )

            fake_openai.state.scripted_tool_calls = [
                {
                    "name": PROPOSE_TOOL_NAME,
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]

            sub = services.sessions.subscribe(project_id, "onboarding", after_seq=None)
            await anext(sub)
            await services.sessions.send(project_id, "onboarding", "Propose connections")
            await collect_until_turn_waiting(sub)

            [approval_id] = await _pending_approvals(services, project_id, 1)

            # Reject with note
            fake_openai.state.scripted_tool_calls = None
            await services.approvals.decide(
                project_id, approval_id, approve=False, note="Not ready yet"
            )

            events = await collect_until_turn_ended(sub)
            ended = [e for e in events if isinstance(e, TurnEnded)]
            assert ended[-1].data.status == "done"

            # Proposal status is rejected
            draft = await services.proposals.get_by_approval(project_id, approval_id)
            assert draft is not None
            assert draft.status == "rejected"


@pytest.mark.asyncio
async def test_failed_part_gives_is_error_naming_the_part(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")

        test_kind = ScriptedTestKind(CONNECTIONS_KIND)
        test_kind.apply_result = ApplyResult(
            applied=["conn_hotel"], failed={"s3_conn": "Bucket does not exist"}
        )
        with swapped_kind(services.proposal_kinds, test_kind):
            onb = services.catalog.get("onboarding")
            services.catalog._by_name["onboarding"] = onb.model_copy(  # pyright: ignore[reportPrivateUsage]
                update={"tools": (*onb.tools, PROPOSE_TOOL_NAME)}
            )

            fake_openai.state.scripted_tool_calls = [
                {
                    "name": PROPOSE_TOOL_NAME,
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]

            sub = services.sessions.subscribe(project_id, "onboarding", after_seq=None)
            await anext(sub)
            await services.sessions.send(project_id, "onboarding", "Propose connections")
            await collect_until_turn_waiting(sub)

            [approval_id] = await _pending_approvals(services, project_id, 1)

            fake_openai.state.scripted_tool_calls = None
            await services.approvals.decide(project_id, approval_id, approve=True, note=None)

            events = await collect_until_turn_ended(sub)
            ended = [e for e in events if isinstance(e, TurnEnded)]
            assert ended[-1].data.status == "done"

            # Check proposal result
            draft = await services.proposals.get_by_approval(project_id, approval_id)
            assert draft is not None
            assert draft.result is not None
            assert draft.result.failed == {"s3_conn": "Bucket does not exist"}

            # Check that tool output returned is_error=True naming s3_conn
            import json
            from typing import cast

            from mlops_agents.storage.tables import messages as messages_table

            engine = await services.databases.project(project_id)
            async with engine.connect() as conn:
                msg_rows = (
                    (await conn.execute(select(messages_table).order_by(messages_table.c.seq)))
                    .mappings()
                    .all()
                )
            tool_results: list[dict[str, Any]] = []
            for row in msg_rows:
                raw_parts = cast(list[dict[str, Any]], json.loads(str(row["parts"])))
                for p in raw_parts:
                    if p.get("type") == "tool_result":
                        tool_results.append(p)
            assert tool_results
            failed_result = next(r for r in tool_results if "s3_conn" in r["content"])
            assert failed_result["is_error"] is True
            assert "s3_conn: Bucket does not exist" in failed_result["content"]
            assert "Applied: conn_hotel" in failed_result["content"]


@pytest.mark.asyncio
async def test_two_steps_of_one_turn_proposing_same_kind_read_their_own_result(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)

        test_kind = ScriptedTestKind(CONNECTIONS_KIND)
        with swapped_kind(services.proposal_kinds, test_kind):
            tool = ProposeTool(services.proposals, services.proposal_kinds)

            turn_id = "trn_multistep"
            ctx1 = _dummy_ctx(project_id, turn_id=turn_id, tool_call_id="call_step1")
            ctx2 = _dummy_ctx(project_id, turn_id=turn_id, tool_call_id="call_step2")
            args = ProposeArgs.model_validate({"kind": CONNECTIONS_KIND, "payload": connections()})

            # Step 1 proposal
            await tool.preview(ctx1, args)
            p1 = await services.proposals.for_call(project_id, turn_id, "call_step1")
            assert p1 is not None

            # Step 1 approved and applied with result 1
            test_kind.apply_result = ApplyResult(applied=["conn_step1"], failed={})
            await services.proposals.decide(project_id, p1.id, approved=True)

            # Step 2 proposal in the same turn with the same kind
            await tool.preview(ctx2, args)
            p2 = await services.proposals.for_call(project_id, turn_id, "call_step2")
            assert p2 is not None
            assert p2.id != p1.id

            # Step 2 approved and applied with result 2
            test_kind.apply_result = ApplyResult(
                applied=[], failed={"conn_step2": "Failed in step 2"}
            )
            await services.proposals.decide(project_id, p2.id, approved=True)

            # Step 1 tool.run reads its OWN proposal result (by tool_call_id), not the newest
            out1 = await tool.run(ctx1, args)
            assert out1.is_error is False
            assert out1.content == "Approved. Everything was applied."

            # Step 2 tool.run reads its OWN proposal result
            out2 = await tool.run(ctx2, args)
            assert out2.is_error is True
            assert "conn_step2: Failed in step 2" in out2.content


# ---------------------------------------------------------------------------
# Credential scrubbing across approvals, audit, events, and checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_scrubbing_after_propose_post_credential_approve_cycle(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
) -> None:
    SECRET_STRING = "VERY_CONFIDENTIAL_TOKEN_SECRET_987654321"

    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        await _use_model(services, fake_openai, "fake-tools")

        test_kind = ScriptedTestKind(
            name=CONNECTIONS_KIND,
            slots=[CredentialSlot(index=0, label="Secret Token", source_type="token")],
        )
        with swapped_kind(services.proposal_kinds, test_kind):
            onb = services.catalog.get("onboarding")
            services.catalog._by_name["onboarding"] = onb.model_copy(  # pyright: ignore[reportPrivateUsage]
                update={"tools": (*onb.tools, PROPOSE_TOOL_NAME)}
            )

            fake_openai.state.scripted_tool_calls = [
                {
                    "name": PROPOSE_TOOL_NAME,
                    "arguments": {"kind": CONNECTIONS_KIND, "payload": connections()},
                }
            ]

            sub = services.sessions.subscribe(project_id, "onboarding", after_seq=None)
            await anext(sub)
            await services.sessions.send(project_id, "onboarding", "Propose with credential slot")
            await collect_until_turn_waiting(sub)

            [approval_id] = await _pending_approvals(services, project_id, 1)

            # Post credential into the slot
            proposal = await services.proposals.get_by_approval(project_id, approval_id)
            assert proposal is not None
            await services.proposals.put_credential(
                project_id, proposal.id, slot=0, value=SECRET_STRING
            )

            # Approve proposal
            fake_openai.state.scripted_tool_calls = None
            await services.approvals.decide(project_id, approval_id, approve=True, note=None)

            events = await collect_until_turn_ended(sub)
            ended = [e for e in events if isinstance(e, TurnEnded)]
            assert ended[-1].data.status == "done"

            # Now search all 4 places:
            # 1. Approvals
            # 2. Audit records
            # 3. Events
            # 4. Checkpoints in database
            engine = await services.databases.project(project_id)
            async with engine.connect() as conn:
                # 1. Approvals table
                appr_rows = (await conn.execute(select(approvals_table))).mappings().all()
                for r in appr_rows:
                    for k, v in r.items():
                        assert SECRET_STRING not in str(v), f"Secret found in approvals column {k}"

                # 2. Audit table
                audit_rows = (await conn.execute(select(audit_table))).mappings().all()
                for r in audit_rows:
                    for k, v in r.items():
                        assert SECRET_STRING not in str(v), f"Secret found in audit column {k}"

                # 3. Events table
                event_rows = (await conn.execute(select(events_table))).mappings().all()
                for r in event_rows:
                    for k, v in r.items():
                        assert SECRET_STRING not in str(v), f"Secret found in events column {k}"

                # 4. Checkpoints tables in sqlite
                from sqlalchemy import text

                tbl_res = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                table_names = [r[0] for r in tbl_res.fetchall()]

                checkpoint_tables = [t for t in table_names if "checkpoint" in t.lower()]
                assert len(checkpoint_tables) > 0, "Expected checkpoint tables in project.db"
                for tname in checkpoint_tables:
                    rows = (await conn.execute(text(f"SELECT * FROM {tname}"))).fetchall()  # noqa: S608 -- test table names
                    for row in rows:
                        for val in row:
                            if isinstance(val, (str, bytes)):
                                str_val = (
                                    val.decode("utf-8", errors="ignore")
                                    if isinstance(val, bytes)
                                    else val
                                )
                                assert SECRET_STRING not in str_val, (
                                    f"Secret found in {tname} table"
                                )


def test_propose_serves_the_code_parser_contract() -> None:
    # The agent writes a code parser's script against the contract its card shows (#634).
    tool = ProposeTool(MagicMock(), MagicMock())
    assert CODE_PARSER_CONTRACT in tool.schema()["function"]["description"]


@pytest.mark.asyncio
async def test_propose_parser_mapping_refuses_ran_at_in_defaults_or_missing_from_fields() -> None:
    from unittest.mock import AsyncMock

    from mlops_agents.monitoring.results.parser_kind import ParserKind
    from mlops_agents.proposals.contract import PARSER_KIND

    parsers_mock = MagicMock()
    proposals_service = MagicMock(spec=ProposalsService)
    proposals_service.create = AsyncMock()
    kinds = ProposalKinds()
    kinds.register(ParserKind(parsers_mock))
    tool = ProposeTool(proposals_service, kinds)
    ctx = _dummy_ctx()

    bad_defaults_payload: dict[str, Any] = {
        "spec": {
            "source_id": "file:conn:out.jsonl",
            "mapping": {
                "format": "jsonl",
                "fields": {"check_id": "id", "ran_at": "ts"},
                "fired_when": {"field": "v", "op": "gt", "value": 1.0},
                "defaults": {"ran_at": "2026-11-01T00:00:00Z"},
            },
        },
        "sample_input": "",
        "sample_records": [],
    }
    with pytest.raises(ToolFailure, match="time the check ran"):
        await tool.preview(ctx, ProposeArgs(kind=PARSER_KIND, payload=bad_defaults_payload))

    bad_fields_payload: dict[str, Any] = {
        "spec": {
            "source_id": "file:conn:out.jsonl",
            "mapping": {
                "format": "jsonl",
                "fields": {"check_id": "id"},
                "fired_when": {"field": "v", "op": "gt", "value": 1.0},
                "defaults": {},
            },
        },
        "sample_input": "",
        "sample_records": [],
    }
    with pytest.raises(ToolFailure, match="time the check ran"):
        await tool.preview(ctx, ProposeArgs(kind=PARSER_KIND, payload=bad_fields_payload))
