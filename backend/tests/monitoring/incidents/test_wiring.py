"""The running app wires SignalsFromResults onto the real Results service."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mlops_agents.api.services import Services
from mlops_agents.monitoring.results.contract import ResultRecord, source_id
from tests.api.conftest import add_local_project, started_app


async def test_fired_result_through_build_services_opens_an_incident(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        project_id = await add_local_project(app, tmp_path, git_env)
        services: Services = app.state.services
        t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
        record = ResultRecord(
            check_id="chk_nulls",
            origin="agent",
            ran_at=t0,
            fired=True,
            category="data_quality",
            subject="customers",
            model="churn",
            detail={"null_count": 12},
            source_ref="results.jsonl:0",
        )
        s_id = source_id("file", "*.jsonl", "data_conn")
        await services.results.ingest(project_id, s_id, [("k1", record)], seen_at=t0)

        await services.results.deliver_undelivered(project_id, t0)

        page = await services.incidents.list(project_id)
        assert page.total == 1
        incident = page.rows[0]
        assert incident.status == "open"
        assert incident.category == "data_quality"
        assert incident.model == "churn"


async def test_incidents_bind_expectations_wired_in_services(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert services.incidents._expectations is services.expectations  # pyright: ignore[reportPrivateUsage]
