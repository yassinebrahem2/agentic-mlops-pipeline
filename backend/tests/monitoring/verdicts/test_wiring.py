"""build_services wires the verdicts: the store, its binding, the provider and the observer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mlops_agents.api.services import Services
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.verdicts.contract import VerdictDraft
from mlops_agents.monitoring.verdicts.labels import LabelConfirmation
from mlops_agents.monitoring.verdicts.query import VerdictsQueryProvider
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from tests.api.conftest import add_local_project, started_app


async def test_build_services_wires_the_verdicts(tmp_path: Path, git_env: dict[str, str]) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert isinstance(services.verdicts, SqliteVerdicts)
        assert isinstance(services.query_providers.get("verdicts"), VerdictsQueryProvider)
        assert any(isinstance(o, LabelConfirmation) for o in services.scheduler.sync_observers)

        project_id = await add_local_project(app, tmp_path, git_env)
        t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)
        outcome = await services.incidents.raise_signal(
            project_id,
            SignalDraft(
                kind="fired_result",
                category="model_performance",
                subject="scores",
                model="churn",
                ran_at=t0,
                detail={},
            ),
            seen_at=t0,
        )
        view = await services.verdicts.record(
            project_id, outcome.incident_id, VerdictDraft(value="real_issue"), by="user"
        )

        incident = await services.incidents.get(project_id, outcome.incident_id)
        assert incident is not None
        assert incident.verdict == view
