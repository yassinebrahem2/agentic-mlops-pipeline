"""Events carry the time of the clock `create_app` was given (bug #641)."""

from __future__ import annotations

from pathlib import Path

from mlops_agents.clock import to_iso
from tests.tracking.conftest import APP_INSTANT, app_at_instant


async def test_events_are_stamped_with_the_app_clock(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with app_at_instant(tmp_path, git_env) as (services, project_id):
        # No GitHub token is set: the sync ends at once with an error and no request, and
        # records its tracking.synced event (ok false).
        source = await services.tracking.create_source(
            project_id, "github_actions", {"repository": "octo/repo"}
        )
        await services.scheduler.run_due(project_id, APP_INSTANT)
        first = await services.events.page(project_id, subject_id=source.id)
        assert len(first) == 1
        assert first[0].type == "tracking.synced"
        assert first[0].payload["ok"] is False

        second = await services.events.record(
            project_id, source="system", type="tracking.synced", subject_id=source.id, payload={}
        )

        events = await services.events.page(project_id, subject_id=source.id)
        assert [e.id for e in events] == [second.id, first[0].id]
        assert [e.ts for e in events] == [to_iso(APP_INSTANT)] * 2
