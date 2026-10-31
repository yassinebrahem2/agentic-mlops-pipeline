"""build_services wires the baseline: team fires are recorded, never signalled or notified."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mlops_agents.api.services import Services
from mlops_agents.monitoring.results.contract import ResultOrigin, ResultRecord, source_id
from mlops_agents.monitoring.verdicts.baseline import SqliteBaseline
from tests.api.conftest import add_local_project, started_app

SOURCE = source_id("file", "*.jsonl", "results_conn")


def _record(check_id: str, origin: ResultOrigin, ran_at: datetime) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin=origin,
        ran_at=ran_at,
        fired=True,
        category="model_performance",
        subject="scores",
        model="churn",
        detail={},
        source_ref=f"results.jsonl:{check_id}",
    )


async def test_team_fires_are_linked_but_never_raise_a_signal_or_a_notification(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert isinstance(services.baseline, SqliteBaseline)
        project_id = await add_local_project(app, tmp_path, git_env)
        now = datetime.now(UTC)

        [team] = await services.results.ingest(
            project_id,
            SOURCE,
            [("t1", _record("team_recall", "team", now - timedelta(minutes=10)))],
            now,
        )
        await services.results.deliver_undelivered(project_id, now)

        assert await services.baseline.available(project_id)
        assert [a.result_id for a in await services.baseline.silent_alerts(project_id)] == [team.id]
        assert (await services.incidents.list(project_id)).total == 0
        types = {e.type for e in await services.events.page(project_id, limit=500)}
        assert "signal.raised" not in types
        assert "incident.opened" not in types
        assert not {t for t in types if t.startswith("notification.")}

        # The control: an agent fire on the same key does open an incident, and the team
        # fire already stored is linked to it.
        await services.results.ingest(
            project_id, SOURCE, [("a1", _record("agent_recall", "agent", now))], now
        )
        await services.results.deliver_undelivered(project_id, now)

        [incident] = (await services.incidents.list(project_id)).rows
        types = {e.type for e in await services.events.page(project_id, limit=500)}
        assert "incident.opened" in types
        [link] = await services.baseline.links(project_id, incident.id)
        assert link.team_check_id == "team_recall"
        assert link.lead_seconds < 0
        assert await services.baseline.silent_alerts(project_id) == []
