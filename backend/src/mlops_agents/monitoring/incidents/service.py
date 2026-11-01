"""IncidentsService implementation of Incidents protocol (I26)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import RowMapping

from mlops_agents.clock import as_utc
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import Expectations, Maintenance
from mlops_agents.monitoring.incidents.contract import (
    ACTIVE_STATUSES,
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
from mlops_agents.monitoring.incidents.grouping import (
    find_matching_active_incident,
    find_reopen_candidate,
)
from mlops_agents.monitoring.incidents.store import (
    IncidentStore,
    parse_dt,
    row_to_incident_view,
)
from mlops_agents.monitoring.incidents.subtype import determine_subtype
from mlops_agents.monitoring.results.contract import Results
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.monitoring.verdicts.contract import Verdicts, VerdictView
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page

if TYPE_CHECKING:
    from mlops_agents.proposals.contract import Handoffs

logger = logging.getLogger(__name__)

SEVERITY_RANKS: Final[dict[Severity, int]] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

# An incident in one of these is finished: no status change takes it, and it merges into nothing.
DONE_STATUSES: Final[tuple[IncidentStatus, ...]] = ("closed", "merged")


def _not_live(incident_id: str, status: str) -> DomainError:
    return DomainError(
        "incident_not_live",
        f"Incident '{incident_id}' is {status}; that change no longer applies to it.",
        status=409,
    )


class IncidentsService(Incidents):
    def __init__(
        self,
        databases: Databases,
        events: EventLog,
        results: Results,
        severity: SeverityPolicy,
        maintenance: Maintenance,
        clock: Clock,
        handoffs: Handoffs | None = None,
    ) -> None:
        self._databases = databases
        self._events = events
        self._results = results
        self._severity = severity
        self._maintenance = maintenance
        self._clock = clock
        self._handoffs = handoffs
        self._store = IncidentStore(databases)
        self._observers: list[IncidentObserver] = []
        self._verdicts: Verdicts | None = None
        self._expectations: Expectations | None = None

    def add_observer(self, observer: IncidentObserver) -> None:
        self._observers.append(observer)

    def bind_verdicts(self, verdicts: Verdicts) -> None:
        self._verdicts = verdicts

    def bind_expectations(self, expectations: Expectations) -> None:
        self._expectations = expectations

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

        settings = await get_monitoring_settings(self._databases, project_id)
        handoff_view = await self._handoffs.current(project_id) if self._handoffs else None
        payload = handoff_view.payload if handoff_view is not None else None

        active_row: RowMapping | None = None
        if not draft.cleared:
            active_row = await find_matching_active_incident(
                self._store,
                project_id,
                draft,
                payload,
                settings.grouping_window_minutes,
            )

        if active_row is not None:
            # Case 1: Attach to active incident
            inc_id = str(active_row["id"])
            sig_id = new_id("sig")
            c_key = cause_key(draft.subject, draft.model)
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

            # Quiet attach: emits no IncidentChange
            return SignalOutcome(
                signal_id=sig_id,
                incident_id=inc_id,
                change="quiet",
                severity=new_sev,
            )

        c_key = cause_key(draft.subject, draft.model)
        if not draft.cleared:
            reopen_row = await find_reopen_candidate(
                self._store,
                project_id,
                c_key,
                draft.ran_at,
                settings.reopen_window_hours,
            )
            if reopen_row is not None:
                # Case 2: Reopen resolved incident
                inc_id = str(reopen_row["id"])
                sig_id = new_id("sig")
                new_reopened_count = int(reopen_row["reopened_count"]) + 1
                await self._store.insert_signal(project_id, sig_id, inc_id, draft, seen_at, c_key)

                cur_sev = reopen_row["severity"]
                await self._store.update_incident(
                    project_id,
                    inc_id,
                    {
                        "status": "reopened",
                        "reopened_count": new_reopened_count,
                        "resolved_at": None,
                        "resolved_offline": 0,
                    },
                )
                await self._events.record(
                    project_id,
                    source="system",
                    type="incident.reopened",
                    payload={
                        "incident_id": inc_id,
                        "reopened_count": new_reopened_count,
                        "cause_key": c_key,
                    },
                    subject_id=inc_id,
                )
                await self._notify_observers(
                    project_id,
                    IncidentChange(
                        incident_id=inc_id,
                        kind="reopened",
                    ),
                )
                return SignalOutcome(
                    signal_id=sig_id,
                    incident_id=inc_id,
                    change="reopened",
                    severity=cur_sev,
                )

        # Case 3: Open new incident
        inc_id = new_id("inc")
        sig_id = new_id("sig")
        subtype = determine_subtype(draft, payload)
        started_at = draft.ran_at
        detected_at = seen_at
        is_resolved = bool(draft.cleared)

        # Link suspects from recent changes within window before started_at
        window_seconds = settings.recent_change_window_hours * 3600
        all_changes = await self._store.list_recent_changes(project_id)
        matching_changes: list[RowMapping] = []
        prs: list[int] = []
        for chg in all_changes:
            chg_at = parse_dt(str(chg["at"]))
            diff = (started_at - chg_at).total_seconds()
            if 0 <= diff <= window_seconds:
                matching_changes.append(chg)
                if chg["kind"] in ("pr", "agent_pr"):
                    try:
                        pr_num = int(chg["ref"])
                        if pr_num not in prs:
                            prs.append(pr_num)
                    except ValueError:
                        pass
        prs.sort()

        await self._store.insert_incident(
            project_id,
            {
                "id": inc_id,
                "category": draft.category,
                "subtype": subtype,
                "severity": "medium",
                "status": "resolved" if is_resolved else "open",
                "cause": None,
                "cause_key": c_key,
                "model": draft.model,
                "prs": json.dumps(prs),
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
            prs=prs,
            signal_ids=[sig_id],
        )
        decision = await self._severity.decide(project_id, temp_view)
        severity = decision.severity

        if severity != "medium":
            await self._store.update_incident(project_id, inc_id, {"severity": severity})

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

        for chg in matching_changes:
            chg_at = parse_dt(str(chg["at"]))
            await self._events.record(
                project_id,
                source="system",
                type="incident.suspect_linked",
                payload={
                    "incident_id": inc_id,
                    "kind": chg["kind"],
                    "ref": chg["ref"],
                    "at": chg_at.isoformat(),
                    "detail": (
                        json.loads(str(chg["detail"]))
                        if isinstance(chg["detail"], str)
                        else chg["detail"]
                    ),
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
        inbox: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]:
        if inbox and status:
            raise DomainError(
                "inbox_status_conflict",
                "Cannot combine 'status' and 'inbox' filters.",
                status=422,
            )
        rows, total = await self._store.list_incidents(
            project_id, status=status, limit=limit, offset=offset, inbox=inbox
        )
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
        if row["status"] not in ACTIVE_STATUSES:
            raise _not_live(incident_id, row["status"])
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
        if row["status"] in DONE_STATUSES:
            raise _not_live(incident_id, row["status"])
        # as_utc: comparing a naive time with an aware one raises.
        if as_utc(until) <= self._clock.now():
            raise DomainError("snooze_in_past", "A snooze must end in the future.", status=422)
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
        if row["status"] in DONE_STATUSES:
            raise _not_live(incident_id, row["status"])
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

    async def record_issue(self, project_id: str, incident_id: str, issue_number: int) -> list[int]:
        row = await self._store.get_incident_row(project_id, incident_id)
        if row is None:
            raise DomainError(
                "incident_not_found",
                f"Incident '{incident_id}' not found.",
                status=404,
            )
        issues: list[int] = json.loads(str(row["issues"])) if row["issues"] else []
        if issue_number not in issues:
            issues.append(issue_number)
            issues.sort()
            await self._store.update_incident(
                project_id, incident_id, {"issues": json.dumps(issues)}
            )
        return issues

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
        if patch.status is not None and row["status"] in DONE_STATUSES:
            raise _not_live(incident_id, row["status"])

        sigs = await self._store.list_signals_for_incident(project_id, incident_id)

        if patch.status == "resolved":
            # Silence signals check
            silence_sigs = [s for s in sigs if s.kind == "silence"]
            if silence_sigs:
                if self._expectations is None:
                    raise DomainError(
                        "incident_still_firing",
                        "Incident has silence signals and cannot be resolved yet.",
                        status=409,
                    )
                states = await self._expectations.states(project_id)
                states_by_key = {(s.key.kind, s.key.ref, s.key.model): s.status for s in states}
                for s in silence_sigs:
                    if s.expectation is not None:
                        st = states_by_key.get(
                            (s.expectation.kind, s.expectation.ref, s.expectation.model)
                        )
                        if st != "ok":
                            raise DomainError(
                                "incident_still_firing",
                                "Expectation is still silent.",
                                status=409,
                            )
                    else:
                        matching = [
                            st
                            for (_k, ref, model), st in states_by_key.items()
                            if ref == s.subject and model == s.model
                        ]
                        if not matching or any(st != "ok" for st in matching):
                            raise DomainError(
                                "incident_still_firing",
                                "Expectation is still silent.",
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
        for r in fetched_rows:
            if r["status"] in DONE_STATUSES:
                raise _not_live(str(r["id"]), r["status"])

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
