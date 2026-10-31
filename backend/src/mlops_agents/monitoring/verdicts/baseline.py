"""The team's own alerts beside the agents' incidents (I31): links, silent alerts, `available`.

The baseline only records: it never notifies, never raises a signal, never opens an incident
(S4 §10). It writes `baseline_links` and `silent_alert_verdicts`, nothing else.

Both observers sit on paths that must not fail: the incident observer is awaited inside
`raise_signal` (paging), the results observer beside incidents' own observer during delivery.
Each catches its own errors and logs them at warning, so a baseline bug never delays either.
"""

from __future__ import annotations

import builtins
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from contextlib import aclosing
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.monitoring.incidents.contract import (
    ACTIVE_STATUSES,
    IncidentChange,
    IncidentObserver,
    Incidents,
    IncidentView,
    cause_key,
)
from mlops_agents.monitoring.incidents.store import parse_dt
from mlops_agents.monitoring.results.constants import QUERY_LIMIT_MAX
from mlops_agents.monitoring.results.contract import (
    ResultQuery,
    Results,
    ResultsObserver,
    StoredResult,
)
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.monitoring.verdicts.contract import (
    Baseline,
    BaselineLink,
    SilentTeamAlert,
    SilentVerdict,
)
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import baseline_links, silent_alert_verdicts

logger = logging.getLogger(__name__)

SILENT_ALERTS_MAX: Final[int] = 500


def is_team_fire(result: StoredResult) -> bool:
    """A team check that fired; a not-enough-data record is never a fire."""
    return result.origin == "team" and result.fired and not result.not_enough_data


def match_window_end(
    incident: IncidentView, *, now: datetime, survivor: IncidentView | None = None
) -> datetime | None:
    """Where an incident's match window ends (Decision #565); None when it matches nothing.

    A live incident ends now, even with an old `resolved_at` (a reopened one); a done one at
    `resolved_at`, else `closed_at` (a person can close an open incident). A merged incident
    ends where its survivor ends.
    """
    if incident.status == "merged":
        if survivor is None or survivor.status == "merged":
            return None
        return match_window_end(survivor, now=now)
    if incident.status in ACTIVE_STATUSES:
        return now
    return incident.resolved_at or incident.closed_at


def team_fire_matches(
    result: StoredResult,
    incident: IncidentView,
    window_minutes: int,
    *,
    now: datetime,
    survivor: IncidentView | None = None,
) -> bool:
    """The one rule that ties a team fire to an incident.

    The fire is on the incident's own cause key (a merged incident keeps its key) and ran in
    `[started_at - window, end]`. `survivor` is used only when the incident is merged.
    """
    if not is_team_fire(result) or cause_key(result.subject, result.model) != incident.cause_key:
        return False
    end = match_window_end(incident, now=now, survivor=survivor)
    if end is None:
        return False
    return incident.started_at - timedelta(minutes=window_minutes) <= result.ran_at <= end


def link_target(incident: IncidentView, by_id: Mapping[str, IncidentView]) -> IncidentView | None:
    """The incident a link goes to: itself, or the first non-merged incident it was merged
    into; None when that chain is broken or loops."""
    seen: set[str] = set()
    current = incident
    while current.status == "merged":
        if current.id in seen or current.merged_into is None:
            return None
        seen.add(current.id)
        following = by_id.get(current.merged_into)
        if following is None:
            return None
        current = following
    return current


class _Matcher:
    """Every incident, read once and indexed by cause key, to match many fires against."""

    def __init__(self, incidents: Iterable[IncidentView], window_minutes: int, now: datetime):
        self._window_minutes = window_minutes
        self._now = now
        self._by_id: dict[str, IncidentView] = {}
        self._by_key: defaultdict[str, list[IncidentView]] = defaultdict(list)
        for incident in incidents:
            self._by_id[incident.id] = incident
            self._by_key[incident.cause_key].append(incident)

    def targets(self, result: StoredResult) -> list[IncidentView]:
        """The incidents a fire links to, each matched incident replaced by its survivor."""
        found: dict[str, IncidentView] = {}
        for incident in self._by_key.get(cause_key(result.subject, result.model), []):
            target = link_target(incident, self._by_id)
            if target is not None and team_fire_matches(
                result, incident, self._window_minutes, now=self._now, survivor=target
            ):
                found[target.id] = target
        return list(found.values())


def _alert(result: StoredResult, verdict: SilentVerdict | None) -> SilentTeamAlert:
    return SilentTeamAlert(
        result_id=result.id,
        check_id=result.check_id,
        subject=result.subject,
        ran_at=result.ran_at,
        verdict=verdict,
    )


class SqliteBaseline(Baseline):
    def __init__(
        self,
        databases: Databases,
        results: Results,
        incidents: Incidents,
        events: EventLog,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._results = results
        self._incidents = incidents
        self._events = events
        self._clock = clock

    async def available(self, project_id: str) -> bool:
        page = await self._results.query(project_id, ResultQuery(origin="team", limit=1))
        return page.total > 0

    async def links(self, project_id: str, incident_id: str) -> builtins.list[BaselineLink]:
        stmt = (
            select(baseline_links)
            .where(baseline_links.c.incident_id == incident_id)
            .order_by(baseline_links.c.first_fired_at, baseline_links.c.team_check_id)
        )
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [
            BaselineLink(
                incident_id=str(r["incident_id"]),
                team_check_id=str(r["team_check_id"]),
                first_fired_at=parse_dt(str(r["first_fired_at"])),
                seen_at=parse_dt(str(r["seen_at"])),
                lead_seconds=float(r["lead_seconds"]),
            )
            for r in rows
        ]

    async def silent_alerts(self, project_id: str) -> builtins.list[SilentTeamAlert]:
        """Team fires no incident matches, newest first, at most SILENT_ALERTS_MAX."""
        silent: builtins.list[StoredResult] = []
        async with aclosing(self._fire_targets(project_id)) as fires:
            async for fire, targets in fires:
                if not targets:
                    silent.append(fire)
                    if len(silent) == SILENT_ALERTS_MAX:
                        break
        verdicts = await self._stored_verdicts(project_id, [r.id for r in silent])
        return [_alert(r, verdicts.get(r.id)) for r in silent]

    async def matched_incidents(self, project_id: str) -> set[str]:
        """Incidents at least one team fire matches now; a merged one counts for its survivor."""
        return {
            target.id async for _, targets in self._fire_targets(project_id) for target in targets
        }

    async def judged_silent_alerts(self, project_id: str) -> builtins.list[SilentTeamAlert]:
        """Every judged team alert still silent now, newest first, uncapped."""
        judged = await self._all_stored_verdicts(project_id)
        if not judged:
            return []
        return [
            _alert(fire, judged[fire.id])
            async for fire, targets in self._fire_targets(project_id)
            if not targets and fire.id in judged
        ]

    async def judge_silent(
        self, project_id: str, result_id: str, value: SilentVerdict, by: str
    ) -> SilentTeamAlert:
        result = await self._silent_alert(project_id, result_id)
        values = {
            "result_id": result_id,
            "value": value,
            "by": by,
            "created": to_iso(self._clock.now()),
        }
        stmt = sqlite_insert(silent_alert_verdicts).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[silent_alert_verdicts.c.result_id],
            set_={k: v for k, v in values.items() if k != "result_id"},
        )
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(stmt)
        await self._events.record(
            project_id,
            source=by,
            type="verdict.recorded",
            payload={"value": value, "source": "user", "kind": "silent_alert"},
            subject_id=result_id,
        )
        return _alert(result, value)

    async def link_fires(self, project_id: str, results: Sequence[StoredResult]) -> None:
        """Links newly stored team fires to the incidents they match."""
        fires = [r for r in results if is_team_fire(r)]
        if not fires:
            return
        matcher = await self._matcher(project_id)
        await self._link(project_id, [(t, f) for f in fires for t in matcher.targets(f)])

    async def look_back(self, project_id: str, incident_id: str) -> None:
        """Links the team fires already stored to an incident just opened or reopened."""
        incident = await self._incidents.get(project_id, incident_id)
        if incident is None:
            return
        window = await self._window_minutes(project_id)
        now = self._clock.now()
        query = ResultQuery(
            origin="team",
            fired=True,
            model=incident.model,
            since=incident.started_at - timedelta(minutes=window),
            limit=QUERY_LIMIT_MAX,
        )
        pairs: builtins.list[tuple[IncidentView, StoredResult]] = []
        offset = 0
        while True:
            page = await self._results.query(
                project_id, query.model_copy(update={"offset": offset})
            )
            pairs.extend(
                (incident, r) for r in page.rows if team_fire_matches(r, incident, window, now=now)
            )
            if not page.truncated or not page.rows:
                break
            offset += len(page.rows)
        await self._link(project_id, pairs)

    async def _window_minutes(self, project_id: str) -> int:
        settings = await get_monitoring_settings(self._databases, project_id)
        return settings.grouping_window_minutes

    async def _silent_alert(self, project_id: str, result_id: str) -> StoredResult:
        found = await self._results.query(project_id, ResultQuery(ids=[result_id], limit=1))
        result = found.rows[0] if found.rows else None
        if result is not None and is_team_fire(result):
            matcher = await self._matcher(project_id)
            if not matcher.targets(result):
                return result
        raise DomainError(
            "silent_alert_not_found",
            f"Result '{result_id}' is not a silent team alert.",
            status=404,
        )

    async def _fire_targets(
        self, project_id: str
    ) -> AsyncGenerator[tuple[StoredResult, builtins.list[IncidentView]]]:
        """Every stored team fire, newest first, with the incidents it matches now (each
        merged one replaced by its survivor); an empty list is a silent alert."""
        matcher = await self._matcher(project_id)
        offset = 0
        while True:
            page = await self._results.query(
                project_id,
                ResultQuery(origin="team", fired=True, limit=QUERY_LIMIT_MAX, offset=offset),
            )
            for result in page.rows:
                if is_team_fire(result):
                    yield result, matcher.targets(result)
            if not page.truncated or not page.rows:
                return
            offset += len(page.rows)

    async def _matcher(self, project_id: str) -> _Matcher:
        window = await self._window_minutes(project_id)
        now = self._clock.now()
        incidents: builtins.list[IncidentView] = []
        offset = 0
        while True:
            page = await self._incidents.list(project_id, limit=QUERY_LIMIT_MAX, offset=offset)
            incidents.extend(page.rows)
            if not page.truncated or not page.rows:
                return _Matcher(incidents, window, now)
            offset += len(page.rows)

    async def _link(
        self, project_id: str, pairs: Sequence[tuple[IncidentView, StoredResult]]
    ) -> None:
        """Upserts one link per (incident, team check), keeping its earliest fire.

        Delivery is ordered only within a batch, so an earlier fire can come later; it then
        replaces the first fire, its `seen_at` and the lead together.
        """
        if not pairs:
            return
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            for incident, fire in pairs:
                stmt = sqlite_insert(baseline_links).values(
                    incident_id=incident.id,
                    team_check_id=fire.check_id,
                    first_fired_at=to_iso(fire.ran_at),
                    seen_at=to_iso(fire.seen_at),
                    lead_seconds=(fire.ran_at - incident.detected_at).total_seconds(),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[baseline_links.c.incident_id, baseline_links.c.team_check_id],
                    set_={
                        "first_fired_at": stmt.excluded.first_fired_at,
                        "seen_at": stmt.excluded.seen_at,
                        "lead_seconds": stmt.excluded.lead_seconds,
                    },
                    # Both sides are `to_iso` text, so the text order is the time order.
                    where=stmt.excluded.first_fired_at < baseline_links.c.first_fired_at,
                )
                await conn.execute(stmt)

    async def _all_stored_verdicts(self, project_id: str) -> dict[str, SilentVerdict]:
        stmt = select(silent_alert_verdicts.c.result_id, silent_alert_verdicts.c.value)
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {str(row.result_id): row.value for row in rows}

    async def _stored_verdicts(
        self, project_id: str, result_ids: Sequence[str]
    ) -> dict[str, SilentVerdict]:
        if not result_ids:
            return {}
        stmt = select(silent_alert_verdicts.c.result_id, silent_alert_verdicts.c.value).where(
            silent_alert_verdicts.c.result_id.in_(result_ids)
        )
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {str(row.result_id): row.value for row in rows}


class BaselineResultsObserver(ResultsObserver):
    """Links newly stored team fires; its own failure never fails the delivery."""

    def __init__(self, baseline: SqliteBaseline) -> None:
        self._baseline = baseline

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        try:
            await self._baseline.link_fires(project_id, results)
        except Exception:
            logger.warning(
                "Baseline links failed for results %s in project %s",
                ", ".join(r.id for r in results),
                project_id,
                exc_info=True,
            )


class BaselineIncidentObserver(IncidentObserver):
    """On `opened` and `reopened`, looks back for team fires already stored; its own failure
    never fails `raise_signal`, which awaits it while paging."""

    def __init__(self, baseline: SqliteBaseline) -> None:
        self._baseline = baseline

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        if change.kind not in ("opened", "reopened"):
            return
        try:
            await self._baseline.look_back(project_id, change.incident_id)
        except Exception:
            logger.warning(
                "Baseline look-back failed for incident %s in project %s",
                change.incident_id,
                project_id,
                exc_info=True,
            )
