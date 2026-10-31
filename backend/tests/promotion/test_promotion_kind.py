"""Tests for PromotionKind and PromotionRejectObserver (Task 3, #497)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.promotion.contract import (
    PromotionPayload,
    Promotions,
    PromotionView,
)
from mlops_agents.promotion.kind import PromotionKind
from mlops_agents.promotion.promotions import PromotionRejectObserver
from mlops_agents.proposals.contract import PROMOTION_KIND, ApplyResult
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore

PROJECT_ID = "prj_promokindtest"


class FakePromotions(Promotions):
    def __init__(self) -> None:
        self.validate_refusals: list[str] = []
        self.apply_result: ApplyResult = ApplyResult(applied=["promotion"], failed={})
        self.history_records: list[PromotionView] = []
        self.rejections: list[tuple[str, str, PromotionPayload]] = []

    async def validate(self, project_id: str, payload: PromotionPayload) -> list[str]:
        return list(self.validate_refusals)

    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]:
        return list(self.history_records)

    async def apply(
        self, project_id: str, proposal_id: str, payload: PromotionPayload
    ) -> ApplyResult:
        return self.apply_result

    async def record_rejection(
        self, project_id: str, proposal_id: str, payload: PromotionPayload
    ) -> None:
        self.rejections.append((project_id, proposal_id, payload))


class FakeWorkspace:
    def __init__(self, projects: list[str]) -> None:
        self._projects = [type("Project", (), {"id": pid}) for pid in projects]

    async def list_projects(self) -> list[Any]:
        return self._projects


@pytest.fixture
def sample_payload() -> PromotionPayload:
    return PromotionPayload(
        model="churn_predictor",
        report_id="rep_123",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy",
    )


def test_promotion_kind_pure(sample_payload: PromotionPayload) -> None:
    promotions_mock = FakePromotions()
    kind = PromotionKind(promotions_mock)  # pyright: ignore[reportArgumentType]

    assert kind.name == PROMOTION_KIND
    assert kind.payload_model is PromotionPayload
    assert kind.key(sample_payload) == "churn_predictor"
    assert kind.credentials_needed(sample_payload) == []

    warnings = kind.warnings(sample_payload)
    assert len(warnings) == 1
    assert "deploy hook" in warnings[0].lower()

    preview = kind.preview(sample_payload)
    assert isinstance(preview, Preview)
    assert "churn_predictor" in preview.title
    assert "2.0.0" in preview.title


@pytest.mark.asyncio
async def test_promotion_kind_check_and_apply(sample_payload: PromotionPayload) -> None:
    promotions_mock = FakePromotions()
    kind = PromotionKind(promotions_mock)  # pyright: ignore[reportArgumentType]

    promotions_mock.validate_refusals = ["Deploy hook missing"]
    refusals = await kind.check(PROJECT_ID, sample_payload)
    assert refusals == ["Deploy hook missing"]

    promotions_mock.apply_result = ApplyResult(applied=["promotion"], failed={})
    res = await kind.apply(PROJECT_ID, "prp_1", sample_payload)
    assert res.applied == ["promotion"]


@pytest.mark.asyncio
async def test_second_promotion_proposal_supersedes_first(
    databases: Databases, sample_payload: PromotionPayload
) -> None:
    await databases.project(PROJECT_ID)
    keystore = MemoryKeyStore()
    project_secrets = ProjectSecrets(keystore, databases)
    events = SqliteEventLog(databases)
    kinds = ProposalKinds()
    promotions_mock = FakePromotions()
    kinds.register(PromotionKind(promotions_mock))  # pyright: ignore[reportArgumentType]

    proposals_svc = ProposalsService(databases, project_secrets, kinds, events)

    # 1. Propose first promotion
    v1 = await proposals_svc.create(
        PROJECT_ID,
        agent="retraining",
        kind=PROMOTION_KIND,
        key="churn_predictor",
        payload=sample_payload.model_dump(),
        turn_id="turn_1",
        tool_call_id="call_1",
    )
    superseded1 = await proposals_svc.attach(PROJECT_ID, v1.id, "apr_1")
    assert superseded1 is None

    p1 = await proposals_svc.pending(PROJECT_ID, PROMOTION_KIND, "churn_predictor")
    assert p1 is not None
    assert p1.id == v1.id

    # 2. Propose second promotion for the same model (newer candidate version)
    payload2 = sample_payload.model_copy(update={"candidate_version": "2.1.0"})
    v2 = await proposals_svc.create(
        PROJECT_ID,
        agent="retraining",
        kind=PROMOTION_KIND,
        key="churn_predictor",
        payload=payload2.model_dump(),
        turn_id="turn_2",
        tool_call_id="call_2",
    )
    superseded2 = await proposals_svc.attach(PROJECT_ID, v2.id, "apr_2")
    # attach returns superseded approval id
    assert superseded2 == "apr_1"

    # Verify first proposal is now superseded and second is pending
    first = await proposals_svc.get_by_approval(PROJECT_ID, "apr_1")
    assert first is not None
    assert first.status == "superseded"

    second = await proposals_svc.get_by_approval(PROJECT_ID, "apr_2")
    assert second is not None
    assert second.status == "pending"


@pytest.mark.asyncio
async def test_reject_observer_records_rejection_only_on_rejected(
    databases: Databases,
    sample_payload: PromotionPayload,
) -> None:
    await databases.project(PROJECT_ID)
    keystore = MemoryKeyStore()
    project_secrets = ProjectSecrets(keystore, databases)
    events = SqliteEventLog(databases)
    kinds = ProposalKinds()
    promotions_mock = FakePromotions()
    kinds.register(PromotionKind(promotions_mock))  # pyright: ignore[reportArgumentType]

    proposals_svc = ProposalsService(databases, project_secrets, kinds, events)
    workspace = FakeWorkspace([PROJECT_ID])
    observer = PromotionRejectObserver(promotions_mock, proposals_svc, workspace)  # pyright: ignore[reportArgumentType]

    # Create and attach proposal
    v = await proposals_svc.create(
        PROJECT_ID,
        agent="retraining",
        kind=PROMOTION_KIND,
        key="churn_predictor",
        payload=sample_payload.model_dump(),
        turn_id="turn_1",
        tool_call_id="call_1",
    )
    await proposals_svc.attach(PROJECT_ID, v.id, "apr_test")

    # 1. A Stop (cancelled) records nothing
    stop_view = ApprovalView(
        id="apr_test",
        agent="retraining",
        session_id="s1",
        turn_id="turn_1",
        kind="tool",
        tool_call_id="call_1",
        tool="Propose",
        risk_class=RiskClass.PROPOSE,
        step=1,
        proposal_kind=PROMOTION_KIND,
        preview=Preview(kind="text", title="t", body="b"),
        status="cancelled",
        note="User stopped session",
        created_at=datetime.now(UTC).isoformat(),
        decided_at=datetime.now(UTC).isoformat(),
    )
    await observer.decided(stop_view)
    assert len(promotions_mock.rejections) == 0

    # 2. A Reject records rejected once
    reject_view = stop_view.model_copy(update={"status": "rejected", "note": "Not ready"})
    await observer.decided(reject_view)
    assert len(promotions_mock.rejections) == 1
    proj, prop_id, p = promotions_mock.rejections[0]
    assert proj == PROJECT_ID
    assert prop_id == v.id
    assert p.model == sample_payload.model

    # Decided told again:
    await observer.decided(reject_view)
    assert (
        len(promotions_mock.rejections) == 2
    )  # The mock records calls; real SqlitePromotions is idempotent
