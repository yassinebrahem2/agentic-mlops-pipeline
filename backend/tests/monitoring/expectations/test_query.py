"""Tests for ResultsQueryProvider and ExpectationsQueryProvider."""

from datetime import UTC, datetime

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationState
from mlops_agents.monitoring.expectations.query import (
    ExpectationsQueryProvider,
    ResultsQueryProvider,
)
from mlops_agents.monitoring.query import MonitoringQuery
from mlops_agents.monitoring.results.contract import ResultQuery, StoredResult
from mlops_agents.tracking.contract import Page


class FakeResults:
    def __init__(self) -> None:
        self.last_query: ResultQuery | None = None

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        self.last_query = q
        now = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
        records = [
            StoredResult(
                id=f"res_{i}",
                check_id="chk_test",
                origin="agent",
                ran_at=now,
                fired=False,
                category="data_quality",
                subject="table_a",
                model=q.model,
                detail={},
                source_ref="s1",
                source_id="file:s1",
                source_key=f"k_{i}",
                seen_at=now,
            )
            for i in range(q.limit)
        ]
        return Page(rows=records, total=100, truncated=True)


class FakeExpectations:
    def __init__(self) -> None:
        self.mock_states = [
            ExpectationState(
                key=ExpectationKey(
                    kind="feed", ref=f"feed_{i}", model="churn" if i % 2 == 0 else "fraud"
                ),
                category="data_quality",
                active=True,
                status="ok" if i % 2 == 0 else "due",
                since=datetime(2026, 10, 31, 10, i, tzinfo=UTC),
            )
            for i in range(10)
        ]

    async def states(self, project_id: str) -> list[ExpectationState]:
        return self.mock_states


async def test_results_query_provider() -> None:
    fake_res = FakeResults()
    provider = ResultsQueryProvider(fake_res)  # type: ignore
    assert provider.what == "results"

    q = MonitoringQuery(
        what="results",
        id="res_42",
        model="churn",
        subject="table_a",
        limit=20,
        offset=10,
    )
    page = await provider.query("prj_1", q)
    assert fake_res.last_query is not None
    assert fake_res.last_query.ids == ["res_42"]
    assert fake_res.last_query.model == "churn"
    assert fake_res.last_query.subject == "table_a"
    assert fake_res.last_query.limit == 20
    assert fake_res.last_query.offset == 10
    assert len(page.rows) == 20
    assert page.total == 100


async def test_expectations_query_provider() -> None:
    fake_exp = FakeExpectations()
    provider = ExpectationsQueryProvider(fake_exp)  # type: ignore
    assert provider.what == "expectations"

    # Filter by model="churn" and status="ok"
    q = MonitoringQuery(
        what="expectations",
        model="churn",
        status="ok",
        limit=5,
        offset=0,
    )
    page = await provider.query("prj_1", q)
    assert page.total == 5
    assert len(page.rows) == 5
    for item in page.rows:
        assert item.key.model == "churn"
        assert item.status == "ok"

    # Offset paging
    q2 = MonitoringQuery(what="expectations", limit=3, offset=3)
    page2 = await provider.query("prj_1", q2)
    assert page2.total == 10
    assert len(page2.rows) == 3
    assert page2.truncated is True
