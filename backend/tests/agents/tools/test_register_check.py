"""RegisterCheck refuses an MLflow metric another check already reads (bug #628)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

import pytest

from mlops_agents.agents.tools.base import FileAccess
from mlops_agents.agents.tools.register_check import RegisterCheck, RegisterCheckInput
from mlops_agents.monitoring.expectations.contract import CheckView
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import Cadence, CheckSpec, FileResults, MlflowResults
from tests.agents.ctx import tool_context
from tests.monitoring.results.conftest import FakeHandoffs, build_mlflow_handoff

CTX = tool_context(cast(FileAccess, None), agent="model-performance")
CADENCE = Cadence(every_minutes=60, cron=None)
NOW = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)


class FakeChecks:
    """The registered checks, in memory; register records each call."""

    def __init__(self, views: list[CheckView] | None = None) -> None:
        self.views = views or []
        self.registered: list[str] = []

    async def register(
        self,
        project_id: str,
        *,
        check_id: str,
        category: ResultCategory,
        cadence: Cadence,
        timezone: str,
        results_location: FileResults | MlflowResults,
        pr_number: int,
        agent: str,
    ) -> CheckView:
        self.registered.append(check_id)
        return _view(check_id, results_location, status="pending_pr", pr_number=pr_number)

    async def list(self, project_id: str) -> list[CheckView]:
        return list(self.views)

    async def list_active(self, project_id: str) -> list[CheckView]:
        return [v for v in self.views if v.status == "active"]

    async def reconcile(self, project_id: str) -> None:
        return None


def _view(
    check_id: str,
    location: FileResults | MlflowResults,
    *,
    status: Literal["pending_pr", "active", "retired"],
    pr_number: int = 7,
) -> CheckView:
    return CheckView(
        id=f"chk_{check_id}",
        check_id=check_id,
        category="model_performance",
        cadence=CADENCE,
        timezone="UTC",
        results_location=location,
        pr_number=pr_number,
        agent="model-performance",
        status=status,
        registered=NOW,
    )


def _args(check_id: str, metric: str) -> RegisterCheckInput:
    return RegisterCheckInput(
        check_id=check_id,
        category="model_performance",
        cadence=CADENCE,
        timezone="UTC",
        results_location=MlflowResults(metric=metric, threshold=0.8, fires_when="below"),
        pr_number=9,
    )


def _handoff_checking(metric: str) -> HandoffView:
    return build_mlflow_handoff(
        [
            CheckSpec(
                check_id="chk_handoff",
                name="Handoff check",
                category="model_performance",
                cadence=CADENCE,
                timezone="UTC",
                results=MlflowResults(metric=metric, threshold=0.5, fires_when="below"),
            )
        ]
    )


@pytest.mark.parametrize(
    ("handoff", "views", "named"),
    [
        pytest.param(_handoff_checking("recall"), [], "handoff check 'chk_handoff'", id="handoff"),
        pytest.param(
            build_mlflow_handoff(
                results_location=MlflowResults(metric="recall", threshold=None, fires_when=None)
            ),
            [],
            "the handoff's results location",
            id="handoff-location",
        ),
        pytest.param(
            None,
            [
                _view(
                    "mp_other",
                    MlflowResults(metric="recall", threshold=None, fires_when=None),
                    status="pending_pr",
                )
            ],
            "registered check 'mp_other'",
            id="pending-check",
        ),
        pytest.param(
            None,
            [
                _view(
                    "mp_other",
                    MlflowResults(metric="recall", threshold=None, fires_when=None),
                    status="active",
                )
            ],
            "registered check 'mp_other'",
            id="active-check",
        ),
    ],
)
async def test_refuses_a_metric_another_check_already_uses(
    handoff: HandoffView | None, views: list[CheckView], named: str
) -> None:
    checks = FakeChecks(views)
    tool = RegisterCheck(checks, FakeHandoffs(handoff))

    out = await tool.run(CTX, _args("mp_recall", "recall"))

    assert out.is_error is True
    assert named in out.content
    assert "'recall'" in out.content
    assert checks.registered == []


async def test_reregistering_an_active_mlflow_check_on_its_metric_succeeds() -> None:
    checks = FakeChecks(
        [
            _view(
                "mp_recall",
                MlflowResults(metric="recall", threshold=0.7, fires_when="below"),
                status="active",
            )
        ]
    )
    tool = RegisterCheck(checks, FakeHandoffs(_handoff_checking("recall")))

    out = await tool.run(CTX, _args("mp_recall", "recall"))

    assert out.is_error is False, out.content
    assert checks.registered == ["mp_recall"]


async def test_free_metric_and_retired_users_do_not_block() -> None:
    checks = FakeChecks(
        [
            _view(
                "mp_old",
                MlflowResults(metric="recall", threshold=None, fires_when=None),
                status="retired",
            ),
            _view(
                "mp_file",
                FileResults(connection="local", path_pattern="recall.jsonl"),
                status="active",
            ),
        ]
    )
    tool = RegisterCheck(checks, FakeHandoffs(_handoff_checking("accuracy")))

    out = await tool.run(CTX, _args("mp_recall", "recall"))

    assert out.is_error is False, out.content
    assert checks.registered == ["mp_recall"]


async def test_failing_handoff_read_is_a_tool_error() -> None:
    class _FailingHandoffs(FakeHandoffs):
        async def current(self, project_id: str) -> HandoffView | None:
            raise RuntimeError("handoff store unavailable")

    checks = FakeChecks()
    tool = RegisterCheck(checks, _FailingHandoffs())

    out = await tool.run(CTX, _args("mp_recall", "recall"))

    assert out.is_error is True
    assert "handoff store unavailable" in out.content
    assert checks.registered == []
