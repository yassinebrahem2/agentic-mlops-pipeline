"""IncidentsService implementation of Incidents protocol (I26)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Final

from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import Maintenance
from mlops_agents.monitoring.incidents.contract import (
    HoldScope,
    IncidentChange,
    IncidentObserver,
    IncidentPatch,
    Incidents,
    IncidentStatus,
    IncidentView,
    Severity,
    SeverityPolicy,
    SignalDraft,
    SignalOutcome,
    SignalView,
    cause_key,
)
from mlops_agents.monitoring.incidents.store import (
    IncidentStore,
    parse_dt,
    row_to_incident_view,
)
from mlops_agents.monitoring.results.contract import Results
from mlops_agents.monitoring.verdicts.contract import Verdicts, VerdictView
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page

logger = logging.getLogger(__name__)

SEVERITY_RANKS: Final[dict[Severity, int]] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


class IncidentsService(Incidents):
    def __init__(
        self,
        databases: Databases,
        events: EventLog,
        results: Results,
        severity: SeverityPolicy,
        maintenance: Maintenance,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._events = events
        self._results = results
        self._severity = severity
        self._maintenance = maintenance
        self._clock = clock
        self._store = IncidentStore(databases)
        self._observers: list[IncidentObserver] = []
        self._verdicts: Verdicts | None = None

    def add_observer(self, observer: IncidentObserver) -> None:
        self._observers.append(observer)

    def bind_verdicts(self, verdicts: Verdicts) -> None:
        self._verdicts = verdicts

    async def _notify_observers(self, project_id: str, change: IncidentChange) -> None:
        for observer in self._observers:
            try:
                await observer.changed(project_id, change)
            except Exception:
                logger.exception(
                    "Observer %s failed for incident change %s in project %s",
                    observer,
                    change,
                    project_id,
                )

    async def _get_verdict(self, project_id: str, incident_id: str) -> VerdictView | None:
        if self._verdicts is not None:
            return await self._verdicts.get(project_id, incident_id)
        return None

    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome:
        # Re-delivered result check
        if draft.result_id is not None:
            existing_sig = await self._store.get_signal_by_result_id(project_id, draft.result_id)
            if existing_sig is not None:
                inc = await self.get(project_id, existing_sig.incident_id)
                sev: Severity = inc.severity if inc else "medium"
                return SignalOutcome(
                    signal_id=existing_sig.id,
                    incident_id=existing_sig.incident_id,
                    change="quiet",
                    severity=sev,
                )

        c_key = cause_key(draft.subject, draft.model)
        active_row = await self._store.get_active_incident_by_cause_key(project_id, c_key)

        if active_row is None or draft.cleared:
            # Case A: Open new incident
            inc_id = new_id("inc")
            sig_id = new_id("sig")
            subtype = "silence" if draft.kind == "silence" else None
            started_at = draft.ran_at
            detected_at = seen_at
            is_resolved = bool(draft.cleared)

            temp_view = IncidentView(
                id=inc_id,
                category=draft.category,
                subtype=subtype,
                severity="medium",
                status="resolved" if is_resolved else "open",
                cause_key=c_key,
                model=draft.model,
                started_at=started_at,
                detected_at=detected_at,
                resolved_offline=is_resolved,
            )
            decision = await self._severity.decide(project_id, temp_view)
            severity = decision.severity

            await self._store.insert_incident(
                project_id,
                {
                    "id": inc_id,
                    "category": draft.category,
                    "subtype": subtype,
                    "severity": severity,
                    "status": "resolved" if is_resolved else "open",
                    "cause": None,
                    "cause_key": c_key,
                    "model": draft.model,
                    "prs": json.dumps([]),
                    "issues": json.dumps([]),
                    "held": 0,
                    "merged_into": None,
                    "started_at": started_at.isoformat(),
                    "detected_at": detected_at.isoformat(),
                    "acknowledged_at": None,
                    "mitigated_at": None,
                    "resolved_at": seen_at.isoformat() if is_resolved else None,
                    "closed_at": None,
                    "snoozed_until": None,
                    "resolved_offline": 1 if is_resolved else 0,
                    "reopened_count": 0,
                    "hold_scope": None,
                    "hold_reason": None,
                },
            )
            await self._store.insert_signal(project_id, sig_id, inc_id, draft, seen_at, c_key)

            await self._events.record(
                project_id,
                source="system",
                type="incident.opened",
                payload={
                    "incident_id": inc_id,
                    "category": draft.category,
                    "severity": severity,
                    "cause_key": c_key,
                    "subject": draft.subject,
                    "model": draft.model,
                },
                subject_id=inc_id,
            )

            await self._notify_observers(
                project_id,
                IncidentChange(
                    incident_id=inc_id,
                    kind="opened",
                    before=None,
                    after=severity,
                ),
            )

            return SignalOutcome(
                signal_id=sig_id,
                incident_id=inc_id,
                change="opened",
                severity=severity,
            )

        # Case B: Attach to active incident
        inc_id = str(active_row["id"])
        sig_id = new_id("sig")
        await self._store.insert_signal(project_id, sig_id, inc_id, draft, seen_at, c_key)

        cur_started = parse_dt(str(active_row["started_at"]))
        new_started = min(cur_started, draft.ran_at)

        cur_sev: Severity = active_row["severity"]
        sig_ids = await self._store.get_signal_ids_for_incident(project_id, inc_id)
        verdict = await self._get_verdict(project_id, inc_id)
        cur_view = row_to_incident_view(active_row, sig_ids, verdict)
        cur_view = cur_view.model_copy(update={"started_at": new_started})

        decision = await self._severity.decide(project_id, cur_view)
        sev_raised = SEVERITY_RANKS.get(decision.severity, 0) > SEVERITY_RANKS.get(cur_sev, 0)
        new_sev = decision.severity if sev_raised else cur_sev

        await self._store.update_incident(
            project_id,
            inc_id,
            {"started_at": new_started.isoformat(), "severity": new_sev},
        )

        if sev_raised:
            await self._events.record(
                project_id,
                source="system",
                type="incident.severity_changed",
                payload={"before": cur_sev, "after": new_sev},
                subject_id=inc_id,
            )
            await self._notify_observers(
                project_id,
                IncidentChange(
                    incident_id=inc_id,
                    kind="severity_raised",
                    before=cur_sev,
                    after=new_sev,
                ),
            )

        return SignalOutcome(
            signal_id=sig_id,
            incident_id=inc_id,
            change="attached",
            severity=new_sev,
        )

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            return None
        sig_ids = await self._store.get_signal_ids_for_incident(project_id, incident_id)
        verdict = await self._get_verdict(project_id, incident_id)
        return row_to_incident_view(row, sig_ids, verdict)

    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]:
        rows, total = await self._store.list_incidents(project_id, status, limit, offset)
        inc_ids = [str(r["id"]) for r in rows]
        sig_ids_map = await self._store.get_signal_ids_for_incidents(project_id, inc_ids)
        items: list[IncidentView] = []
        for r in rows:
            inc_id = str(r["id"])
            verdict = await self._get_verdict(project_id, inc_id)
            items.append(row_to_incident_view(r, sig_ids_map.get(inc_id, []), verdict))
        truncated = (offset + len(items)) < total
        return Page(rows=items, total=total, truncated=truncated)

    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        return await self._store.list_signals_for_incident(project_id, incident_id)

    async def holds(self, project_id: str, model: str | None) -> list[HoldScope]:
        rows = await self._store.list_held_incidents(project_id, model)
        scopes: list[HoldScope] = []
        for r in rows:
            scope_raw = r["hold_scope"]
            if scope_raw:
                scopes.append(HoldScope.model_validate_json(str(scope_raw)))
        return scopes

    async def acknowledge(
        self, project_id: str, incident_id: str, by: str = "user"
    ) -> IncidentView:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        now = self._clock.now()
        await self._store.update_incident(
            project_id,
            incident_id,
            {
                "status": "acknowledged",
                "acknowledged_at": now.isoformat(),
            },
        )
        await self._events.record(
            project_id,
            source=by,
            type="incident.acknowledged",
            payload={"incident_id": incident_id},
            subject_id=incident_id,
        )
        await self._notify_observers(
            project_id,
            IncidentChange(incident_id=incident_id, kind="status"),
        )
        view = await self.get(project_id, incident_id)
        if view is None:
            raise RuntimeError(f"Incident {incident_id} not found after acknowledge")
        return view

    async def snooze(
        self, project_id: str, incident_id: str, until: datetime, by: str = "user"
    ) -> IncidentView:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        await self._store.update_incident(
            project_id,
            incident_id,
            {"snoozed_until": until.isoformat()},
        )
        await self._events.record(
            project_id,
            source=by,
            type="incident.snoozed",
            payload={"incident_id": incident_id, "until": until.isoformat()},
            subject_id=incident_id,
        )
        view = await self.get(project_id, incident_id)
        if view is None:
            raise RuntimeError(f"Incident {incident_id} not found after snooze")
        return view

    async def close(self, project_id: str, incident_id: str, by: str = "user") -> IncidentView:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        now = self._clock.now()
        await self._store.update_incident(
            project_id,
            incident_id,
            {
                "status": "closed",
                "closed_at": now.isoformat(),
            },
        )
        await self._events.record(
            project_id,
            source=by,
            type="incident.closed",
            payload={"incident_id": incident_id},
            subject_id=incident_id,
        )
        await self._notify_observers(
            project_id,
            IncidentChange(incident_id=incident_id, kind="status"),
        )
        view = await self.get(project_id, incident_id)
        if view is None:
            raise RuntimeError(f"Incident {incident_id} not found after close")
        return view

    async def record_hold(
        self,
        project_id: str,
        incident_id: str,
        scope: HoldScope,
        reason: str,
        by: str,
    ) -> IncidentView:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        await self._store.update_incident(
            project_id,
            incident_id,
            {
                "held": 1,
                "hold_scope": scope.model_dump_json(),
                "hold_reason": reason,
            },
        )
        await self._events.record(
            project_id,
            source=by,
            type="incident.hold_recorded",
            payload={
                "incident_id": incident_id,
                "scope": scope.model_dump(mode="json"),
                "reason": reason,
            },
            subject_id=incident_id,
        )
        await self._notify_observers(
            project_id,
            IncidentChange(incident_id=incident_id, kind="hold"),
        )
        view = await self.get(project_id, incident_id)
        if view is None:
            raise RuntimeError(f"Incident {incident_id} not found after record_hold")
        return view

    async def set_severity(
        self,
        project_id: str,
        incident_id: str,
        severity: Severity,
        reason: str,
        by: str,
    ) -> IncidentView:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        cur_sev: Severity = row["severity"]
        if severity == cur_sev:
            view = await self.get(project_id, incident_id)
            if view is None:
                raise RuntimeError(f"Incident {incident_id} not found")
            return view

        await self._store.update_incident(project_id, incident_id, {"severity": severity})
        await self._events.record(
            project_id,
            source=by,
            type="incident.severity_changed",
            payload={"before": cur_sev, "after": severity, "reason": reason},
            subject_id=incident_id,
        )
        is_raised = SEVERITY_RANKS.get(severity, 0) > SEVERITY_RANKS.get(cur_sev, 0)
        kind = "severity_raised" if is_raised else "severity_lowered"
        await self._notify_observers(
            project_id,
            IncidentChange(
                incident_id=incident_id,
                kind=kind,
                before=cur_sev,
                after=severity,
            ),
        )
        view = await self.get(project_id, incident_id)
        if view is None:
            raise RuntimeError(f"Incident {incident_id} not found after set_severity")
        return view

    async def update(
        self,
        project_id: str,
        incident_id: str,
        agent: str,
        patch: IncidentPatch,
    ) -> IncidentView:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )

        sigs = await self._store.list_signals_for_incident(project_id, incident_id)

        if patch.status == "resolved":
            # Silence signals check
            if any(s.kind == "silence" for s in sigs):
                raise DomainError(
                    "incident_still_firing",
                    "Incident has silence signals and cannot be resolved yet.",
                    status=409,
                )
            # Latest results check
            subject = sigs[0].subject if sigs else ""
            model = row["model"]
            latest_results = await self._results.latest_per_check(project_id, subject, model)
            if any(r.fired for r in latest_results):
                raise DomainError(
                    "incident_still_firing",
                    "Latest results for this subject and model are still firing.",
                    status=409,
                )

        values_to_update: dict[str, Any] = {}
        now = self._clock.now()

        if patch.status == "mitigated":
            values_to_update["status"] = "mitigated"
            values_to_update["mitigated_at"] = now.isoformat()
        elif patch.status == "resolved":
            values_to_update["status"] = "resolved"
            values_to_update["resolved_at"] = now.isoformat()

        if patch.cause is not None:
            values_to_update["cause"] = patch.cause

        cat_changed = patch.category is not None and patch.category != row["category"]
        sub_changed = patch.subtype is not None and patch.subtype != row["subtype"]

        sev_change: tuple[Severity, Severity] | None = None
        if cat_changed or sub_changed:
            if patch.category is not None:
                values_to_update["category"] = patch.category
            if patch.subtype is not None:
                values_to_update["subtype"] = patch.subtype

            cur_view = await self.get(project_id, incident_id)
            if cur_view is None:
                raise RuntimeError(f"Incident {incident_id} not found")
            updated_view = cur_view.model_copy(
                update={
                    "category": patch.category if patch.category is not None else cur_view.category,
                    "subtype": patch.subtype if patch.subtype is not None else cur_view.subtype,
                }
            )
            decision = await self._severity.decide(project_id, updated_view)
            cur_sev: Severity = row["severity"]
            new_sev = decision.severity
            values_to_update["severity"] = new_sev

            if SEVERITY_RANKS.get(new_sev, 0) > SEVERITY_RANKS.get(cur_sev, 0):
                sev_change = (cur_sev, new_sev)

        if values_to_update:
            await self._store.update_incident(project_id, incident_id, values_to_update)

        changed_fields: dict[str, Any] = {}
        if patch.status is not None:
            changed_fields["status"] = patch.status
        if patch.cause is not None:
            changed_fields["cause"] = patch.cause
        if patch.category is not None:
            changed_fields["category"] = patch.category
        if patch.subtype is not None:
            changed_fields["subtype"] = patch.subtype
        if patch.note is not None:
            changed_fields["note"] = patch.note

        if changed_fields:
            await self._events.record(
                project_id,
                source=agent,
                type="incident.updated",
                payload={"changed": changed_fields},
                subject_id=incident_id,
            )

        if patch.status is not None:
            await self._notify_observers(
                project_id,
                IncidentChange(incident_id=incident_id, kind="status"),
            )

        if cat_changed or sub_changed:
            await self._notify_observers(
                project_id,
                IncidentChange(incident_id=incident_id, kind="cause"),
            )
            if sev_change is not None:
                await self._events.record(
                    project_id,
                    source=agent,
                    type="incident.severity_changed",
                    payload={"before": sev_change[0], "after": sev_change[1]},
                    subject_id=incident_id,
                )
                await self._notify_observers(
                    project_id,
                    IncidentChange(
                        incident_id=incident_id,
                        kind="severity_raised",
                        before=sev_change[0],
                        after=sev_change[1],
                    ),
                )

        view = await self.get(project_id, incident_id)
        if view is None:
            raise RuntimeError(f"Incident {incident_id} not found after update")
        return view

    async def merge(self, project_id: str, ids: list[str], agent: str) -> IncidentView:
        if len(ids) < 2:
            raise DomainError("invalid_merge", "At least two incidents are required to merge.")

        fetched_rows: list[dict[str, Any]] = []
        for inc_id in ids:
            r = await self._store.get_incident_row(project_id, inc_id)
            if r is None:
                raise DomainError(
                    "incident_not_found",
                    f"Incident '{inc_id}' not found.",
                    status=404,
                )
            fetched_rows.append(dict(r))

        # Sort: oldest started_at, then detected_at
        fetched_rows.sort(
            key=lambda r: (
                parse_dt(str(r["started_at"])),
                parse_dt(str(r["detected_at"])),
            )
        )
        winner_row = fetched_rows[0]
        winner_id = str(winner_row["id"])
        absorbed = fetched_rows[1:]
        absorbed_ids = [str(r["id"]) for r in absorbed]

        # Earliest started_at among all
        all_started = [parse_dt(str(r["started_at"])) for r in fetched_rows]
        earliest_started = min(all_started)

        # Move signals to winner
        await self._store.move_all_signals(project_id, absorbed_ids, winner_id)

        # Mark absorbed as merged
        for other in absorbed:
            other_id = str(other["id"])
            await self._store.update_incident(
                project_id,
                other_id,
                {"status": "merged", "merged_into": winner_id},
            )
            await self._events.record(
                project_id,
                source=agent,
                type="incident.merged",
                payload={"merged_into": winner_id},
                subject_id=other_id,
            )
            await self._notify_observers(
                project_id,
                IncidentChange(incident_id=other_id, kind="merged"),
            )

        # Update winner
        await self._store.update_incident(
            project_id,
            winner_id,
            {"started_at": earliest_started.isoformat()},
        )
        await self._events.record(
            project_id,
            source=agent,
            type="incident.merged",
            payload={"absorbed": absorbed_ids},
            subject_id=winner_id,
        )
        await self._notify_observers(
            project_id,
            IncidentChange(incident_id=winner_id, kind="merged"),
        )

        view = await self.get(project_id, winner_id)
        if view is None:
            raise RuntimeError(f"Incident {winner_id} not found after merge")
        return view

    async def split(
        self,
        project_id: str,
        incident_id: str,
        signal_ids: list[str],
        agent: str,
    ) -> IncidentView:
        parent_row = await self._store.get_incident_row(project_id, incident_id)
        if parent_row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )

        all_signals = await self._store.list_signals_for_incident(project_id, incident_id)
        split_signals = [s for s in all_signals if s.id in signal_ids]
        if not split_signals:
            raise DomainError(
                "invalid_split",
                "Named signals do not belong to this incident.",
                status=400,
            )

        new_inc_id = new_id("inc")
        started_at = min(s.ran_at for s in split_signals)
        detected_at = min(s.seen_at for s in split_signals)
        subtype = "silence" if any(s.kind == "silence" for s in split_signals) else None

        temp_view = IncidentView(
            id=new_inc_id,
            category=parent_row["category"],
            subtype=subtype,
            severity="medium",
            status="open",
            cause_key=str(parent_row["cause_key"]),
            model=parent_row["model"],
            started_at=started_at,
            detected_at=detected_at,
        )
        decision = await self._severity.decide(project_id, temp_view)
        severity = decision.severity

        await self._store.insert_incident(
            project_id,
            {
                "id": new_inc_id,
                "category": parent_row["category"],
                "subtype": subtype,
                "severity": severity,
                "status": "open",
                "cause": None,
                "cause_key": parent_row["cause_key"],
                "model": parent_row["model"],
                "prs": json.dumps([]),
                "issues": json.dumps([]),
                "held": 0,
                "merged_into": None,
                "started_at": started_at.isoformat(),
                "detected_at": detected_at.isoformat(),
                "acknowledged_at": None,
                "mitigated_at": None,
                "resolved_at": None,
                "closed_at": None,
                "snoozed_until": None,
                "resolved_offline": 0,
                "reopened_count": 0,
                "hold_scope": None,
                "hold_reason": None,
            },
        )

        await self._store.move_signals(project_id, signal_ids, new_inc_id)

        await self._events.record(
            project_id,
            source=agent,
            type="incident.split",
            payload={"split_to": new_inc_id, "signals": signal_ids},
            subject_id=incident_id,
        )
        await self._notify_observers(
            project_id,
            IncidentChange(incident_id=incident_id, kind="split"),
        )

        await self._events.record(
            project_id,
            source=agent,
            type="incident.split",
            payload={"split_from": incident_id, "signals": signal_ids},
            subject_id=new_inc_id,
        )
        await self._notify_observers(
            project_id,
            IncidentChange(incident_id=new_inc_id, kind="split"),
        )

        view = await self.get(project_id, new_inc_id)
        if view is None:
            raise RuntimeError(f"Incident {new_inc_id} not found after split")
        return view
