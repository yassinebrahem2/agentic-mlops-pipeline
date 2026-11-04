"""The Dashboard builder, over fake stores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.config import RunMode
from mlops_agents.events.contract import AgentStatus
from mlops_agents.monitoring.dashboard import DashboardBuilder, map_stages_of
from mlops_agents.monitoring.expectations.contract import ExpectationState
from mlops_agents.monitoring.incidents.contract import (
    IncidentStatus,
    IncidentView,
    Severity,
    SignalView,
)
from mlops_agents.monitoring.results.contract import ResultQuery, StoredResult
from mlops_agents.monitoring.verdicts.contract import SilentTeamAlert
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.tracking.contract import ModelSummary, Page, TrackingQuery
from tests.proposals.samples import cadence, data_feed, handoff, model

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
PROJECT = "prj_a"

THREE_STAGES: list[dict[str, Any]] = [
    {"name": "ingest", "entry": "ingest.py", "inputs": ["raw"], "outputs": ["bookings"]},
    {
        "name": "prepare",
        "entry": "prepare.py",
        "inputs": ["bookings"],
        "outputs": ["clean", "prep_report"],
    },
    {"name": "train", "entry": "train.py", "inputs": ["clean"], "outputs": ["model"]},
]
EDGES: list[dict[str, str]] = [
    {"from_stage": "ingest", "to_stage": "prepare"},
    {"from_stage": "prepare", "to_stage": "train"},
]


def payload() -> HandoffPayload:
    return HandoffPayload.model_validate(handoff(pipeline=THREE_STAGES, edges=EDGES))


class FakeHandoffs:
    def __init__(self, current: HandoffPayload | None) -> None:
        self._view = (
            None
            if current is None
            else HandoffView(proposal_id="prp_1", version=1, approved=NOW, payload=current)
        )

    async def current(self, project_id: str) -> HandoffView | None:
        return self._view


class FakeTracking:
    def __init__(self, production: dict[str, str | None]) -> None:
        self.production = production

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        assert q.what == "models"
        rows = [
            ModelSummary(
                source_id="mlflow:1",
                name=name,
                production_version=version,
                latest_version=version,
                versions=1,
            )
            for name, version in self.production.items()
        ]
        return Page(rows=rows, total=len(rows), truncated=False)


class FakeIncidents:
    def __init__(self) -> None:
        self.incidents: list[IncidentView] = []
        self.subjects: dict[str, list[str]] = {}
        self.asked_statuses: list[IncidentStatus] | None = None

    def add(
        self,
        incident_id: str,
        *,
        status: IncidentStatus = "open",
        severity: Severity = "medium",
        model: str | None = None,
        subjects: tuple[str, ...] = (),
    ) -> None:
        self.incidents.append(
            IncidentView(
                id=incident_id,
                category="data_quality",
                severity=severity,
                status=status,
                cause_key="k",
                model=model,
                started_at=NOW,
                detected_at=NOW,
            )
        )
        self.subjects[incident_id] = list(subjects)

    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        inbox: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]:
        self.asked_statuses = status
        rows = [i for i in self.incidents if status is None or i.status in status]
        page = rows[offset : offset + limit]
        return Page(rows=page, total=len(rows), truncated=offset + len(page) < len(rows))

    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]:
        return [
            SignalView(
                kind="fired_result",
                category="data_quality",
                subject=subject,
                ran_at=NOW,
                detail={},
                id=f"sig_{incident_id}_{n}",
                incident_id=incident_id,
                seen_at=NOW,
                cause_key="k",
            )
            for n, subject in enumerate(self.subjects[incident_id])
        ]


class FakeExpectations:
    def __init__(self, states: list[ExpectationState]) -> None:
        self._states = states

    async def states(self, project_id: str) -> list[ExpectationState]:
        return self._states


def expectation(ref: str, status: str, kind: str = "feed") -> ExpectationState:
    return ExpectationState.model_validate(
        {
            "key": {"kind": kind, "ref": ref, "model": None},
            "category": "data_quality",
            "active": True,
            "status": status,
        }
    )


def stored(check_id: str, value: float | None, model: str, subject: str = "x") -> StoredResult:
    return StoredResult(
        check_id=check_id,
        origin="team",
        ran_at=NOW,
        fired=False,
        value=value,
        category="model_performance",
        subject=subject,
        model=model,
        detail={},
        source_ref="ref",
        id=f"res_{check_id}_{model}",
        source_id="file:c:p",
        source_key="k",
        seen_at=NOW,
    )


class FakeResults:
    def __init__(self, rows: list[StoredResult]) -> None:
        self.rows = rows
        self.queries: list[ResultQuery] = []

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        self.queries.append(q)
        rows = [
            r
            for r in self.rows
            if (q.model is None or r.model == q.model)
            and (q.subject is None or r.subject == q.subject)
        ]
        return Page(rows=rows[: q.limit], total=len(rows), truncated=False)


class FakeBaseline:
    def __init__(self, available: bool, alerts: list[SilentTeamAlert] | None = None) -> None:
        self._available = available
        self._alerts = alerts or []

    async def available(self, project_id: str) -> bool:
        return self._available

    async def silent_alerts(self, project_id: str) -> list[SilentTeamAlert]:
        return list(self._alerts)


class FakeBoard:
    def __init__(self, statuses: list[AgentStatus]) -> None:
        self.statuses = statuses

    async def snapshot(self, project_id: str) -> list[AgentStatus]:
        return self.statuses


class Rig:
    def __init__(
        self,
        *,
        current: HandoffPayload | None,
        mode: RunMode = RunMode.SERVER,
        states: list[ExpectationState] | None = None,
        baseline: bool = True,
        alerts: list[SilentTeamAlert] | None = None,
    ) -> None:
        self.incidents = FakeIncidents()
        self.results = FakeResults([])
        self.tracking = FakeTracking({})
        self.board = FakeBoard([])
        self.builder = DashboardBuilder(
            handoffs=FakeHandoffs(current),
            tracking=self.tracking,
            incidents=self.incidents,
            expectations=FakeExpectations(
                states
                if states is not None
                else [expectation("bookings", "ok"), expectation("nightly", "ok", "job")]
            ),
            results=self.results,
            baseline=FakeBaseline(baseline, alerts=alerts),
            status_board=self.board,
            mode=mode,
        )


@pytest.mark.asyncio
async def test_subject_shared_by_two_stages_marks_both() -> None:
    rig = Rig(current=payload(), states=[expectation("bookings", "ok")])
    rig.results.rows = [stored("c", 1.0, "cancel", subject="model")]
    rig.incidents.add("inc_1", subjects=("clean",))

    view = await rig.builder.build(PROJECT)

    by_name = {s.name: s for s in view.stages}
    assert [s.name for s in view.stages] == ["ingest", "prepare", "train"]
    # "clean" is an output of prepare and an input of train: both stages are affected.
    assert by_name["prepare"].status == "incident"
    assert by_name["prepare"].incident_ids == ["inc_1"]
    assert by_name["train"].status == "incident"
    assert by_name["ingest"].status == "ok"
    assert by_name["ingest"].incident_ids == []
    assert [(e.from_stage, e.to_stage) for e in view.edges] == [
        ("ingest", "prepare"),
        ("prepare", "train"),
    ]


@pytest.mark.asyncio
async def test_incident_on_an_output_of_stage_two_marks_stage_two_only() -> None:
    rig = Rig(current=payload(), states=[expectation("bookings", "ok")])
    rig.results.rows = [stored("c", 1.0, "cancel", subject="model")]
    rig.incidents.add("inc_1", subjects=("prep_report",))

    view = await rig.builder.build(PROJECT)

    assert [(s.name, s.status, s.incident_ids) for s in view.stages] == [
        ("ingest", "ok", []),
        ("prepare", "incident", ["inc_1"]),
        ("train", "ok", []),
    ]


@pytest.mark.asyncio
async def test_resolved_incident_does_not_mark_a_stage() -> None:
    rig = Rig(current=payload(), states=[expectation("bookings", "ok")])
    rig.incidents.add("inc_1", status="resolved", subjects=("model",))

    view = await rig.builder.build(PROJECT)

    assert all(s.status != "incident" for s in view.stages)


@pytest.mark.asyncio
async def test_silent_feed_marks_its_stage_late() -> None:
    rig = Rig(current=payload(), states=[expectation("bookings", "silent")])

    view = await rig.builder.build(PROJECT)

    # The feed reads the table "bookings", an output of ingest and an input of prepare.
    assert {s.name for s in view.stages if s.status == "late"} == {"ingest", "prepare"}
    assert [e.key.ref for e in view.at_risk] == ["bookings"]


@pytest.mark.asyncio
async def test_expectation_naming_no_stage_marks_nothing() -> None:
    rig = Rig(current=payload(), states=[expectation("elsewhere", "silent")])
    rig.results.rows = [stored("c", 1.0, "cancel", subject="raw")]

    view = await rig.builder.build(PROJECT)

    assert [s.status for s in view.stages] == ["ok", "unknown", "unknown"]
    assert [e.key.ref for e in view.at_risk] == ["elsewhere"]


@pytest.mark.asyncio
async def test_incident_wins_over_late() -> None:
    rig = Rig(current=payload(), states=[expectation("bookings", "late")])
    rig.incidents.add("inc_1", subjects=("raw",))

    view = await rig.builder.build(PROJECT)

    assert view.stages[0].status == "incident"
    assert view.stages[1].status == "late"


@pytest.mark.asyncio
async def test_stage_nothing_is_known_about_is_unknown() -> None:
    rig = Rig(current=payload(), states=[])

    view = await rig.builder.build(PROJECT)

    assert [s.status for s in view.stages] == ["unknown", "unknown", "unknown"]


@pytest.mark.asyncio
async def test_expectation_not_judged_yet_leaves_its_stage_unknown() -> None:
    rig = Rig(current=payload(), states=[expectation("bookings", "not_judged")])

    view = await rig.builder.build(PROJECT)

    assert [s.status for s in view.stages] == ["unknown", "unknown", "unknown"]


@pytest.mark.asyncio
async def test_a_stored_result_on_a_stage_subject_makes_it_known() -> None:
    rig = Rig(current=payload(), states=[])
    rig.results.rows = [stored("c", 1.0, "cancel", subject="raw")]

    view = await rig.builder.build(PROJECT)

    assert [s.status for s in view.stages] == ["ok", "unknown", "unknown"]


@pytest.mark.asyncio
async def test_no_handoff_gives_empty_map_and_models() -> None:
    rig = Rig(current=None)

    view = await rig.builder.build(PROJECT)

    assert view.stages == []
    assert view.edges == []
    assert view.models == []


@pytest.mark.asyncio
async def test_severity_counts_have_every_key_and_leave_out_inactive_incidents() -> None:
    rig = Rig(current=payload())
    rig.incidents.add("inc_1", severity="critical")
    rig.incidents.add("inc_2", severity="critical", status="acknowledged")
    rig.incidents.add("inc_3", severity="low", status="reopened")
    rig.incidents.add("inc_4", severity="high", status="resolved")
    rig.incidents.add("inc_5", severity="high", status="closed")
    rig.incidents.add("inc_6", severity="high", status="merged")

    view = await rig.builder.build(PROJECT)

    assert view.incidents_by_severity == {"critical": 2, "high": 0, "medium": 0, "low": 1}
    assert list(view.incidents_by_severity) == ["critical", "high", "medium", "low"]


@pytest.mark.asyncio
async def test_every_active_incident_is_counted_across_pages() -> None:
    rig = Rig(current=payload())
    for n in range(620):
        rig.incidents.add(f"inc_{n}", severity="low")

    view = await rig.builder.build(PROJECT)

    assert view.incidents_by_severity["low"] == 620


@pytest.mark.asyncio
async def test_each_model_gets_a_card_with_its_metrics_and_incident_count() -> None:
    rig = Rig(current=payload())
    rig.tracking.production = {"cancel": "7", "noshow": None}
    rig.results.rows = [
        stored("auc", 0.91, "cancel"),
        stored("psi", 0.2, "cancel"),
        stored("auc", 0.7, "noshow"),
        stored("empty", None, "noshow"),
    ]
    rig.incidents.add("inc_1", model="cancel")
    rig.incidents.add("inc_2", model="cancel", status="resolved")
    rig.incidents.add("inc_3", model="noshow")

    view = await rig.builder.build(PROJECT)

    cards = {m.name: m for m in view.models}
    assert list(cards) == ["cancel", "noshow"]
    assert cards["cancel"].production_version == "7"
    assert cards["cancel"].latest_metrics == {"auc": 0.91, "psi": 0.2}
    assert cards["cancel"].open_incidents == 1
    assert cards["noshow"].production_version is None
    assert cards["noshow"].latest_metrics == {"auc": 0.7}
    assert cards["noshow"].open_incidents == 1
    assert all(q.latest for q in rig.results.queries if q.model is not None)


@pytest.mark.asyncio
async def test_at_risk_holds_due_late_and_silent_only() -> None:
    states = [
        expectation("a", "ok"),
        expectation("b", "due"),
        expectation("c", "late"),
        expectation("d", "silent"),
        expectation("e", "suspended"),
        expectation("f", "pending_pr"),
    ]
    rig = Rig(current=payload(), states=states)

    view = await rig.builder.build(PROJECT)

    assert [e.key.ref for e in view.at_risk] == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_agents_show_what_each_is_doing() -> None:
    rig = Rig(current=payload())
    task = TaskView(
        id="tsk_1",
        agent="coder",
        title="Fix the parser",
        branch="b",
        status="active",
        review_kind=None,
        pr_number=None,
        pr_url=None,
    )
    rig.board.statuses = [
        AgentStatus(
            agent="coder",
            display_name="Coder",
            state="running",
            task=task,
            open_prs=[],
            last_finished=None,
        ),
        AgentStatus(
            agent="analyst",
            display_name="Analyst",
            state="idle",
            task=None,
            open_prs=[],
            last_finished=None,
        ),
    ]

    view = await rig.builder.build(PROJECT)

    assert [(a.agent, a.state, a.doing) for a in view.agents] == [
        ("coder", "running", "Fix the parser"),
        ("analyst", "idle", None),
    ]


@pytest.mark.asyncio
async def test_local_mode_cannot_be_reached_by_the_heartbeat() -> None:
    local = await Rig(current=payload(), mode=RunMode.LOCAL).builder.build(PROJECT)
    server = await Rig(current=payload(), mode=RunMode.SERVER).builder.build(PROJECT)

    assert local.heartbeat_reachable is False
    assert server.heartbeat_reachable is True


@pytest.mark.asyncio
async def test_baseline_available_follows_the_baseline() -> None:
    yes = await Rig(current=payload(), baseline=True).builder.build(PROJECT)
    no = await Rig(current=payload(), baseline=False).builder.build(PROJECT)

    assert yes.baseline_available is True
    assert no.baseline_available is False


@pytest.mark.asyncio
async def test_dashboard_team_alerts_sorted_newest_first_and_capped_at_ten() -> None:
    alerts = [
        SilentTeamAlert(
            result_id=f"res_{i}",
            check_id=f"chk_{i}",
            subject="clean",
            ran_at=datetime(2026, 10, 30, 10, i, tzinfo=UTC),
            category="data_quality",
        )
        for i in range(15)
    ]
    rig = Rig(current=payload(), alerts=alerts)
    view = await rig.builder.build(PROJECT)
    assert len(view.team_alerts) == 10
    assert [a.result_id for a in view.team_alerts] == [f"res_{i}" for i in range(14, 4, -1)]


def walkthrough_3_payload() -> HandoffPayload:
    return HandoffPayload.model_validate(
        handoff(
            models=[
                model(
                    name="hotel-cancellation",
                    predictions={"connection": "hotel", "table": "predictions"},
                    labels=None,
                )
            ],
            pipeline=[
                {
                    "name": "bookings",
                    "entry": "ingest.py",
                    "inputs": ["raw"],
                    "outputs": ["bookings"],
                },
                {
                    "name": "predictions",
                    "entry": "score.py",
                    "inputs": ["features"],
                    "outputs": ["predictions"],
                },
                {
                    "name": "monitoring",
                    "entry": "monitor.py",
                    "inputs": ["metrics"],
                    "outputs": ["monitoring"],
                },
            ],
            edges=[
                {"from_stage": "bookings", "to_stage": "predictions"},
                {"from_stage": "predictions", "to_stage": "monitoring"},
            ],
            operations={
                "scheduler": {"name": "cron", "config_location": "cron"},
                "feeds": [
                    {
                        "name": "daily-scoring",
                        "kind": "job",
                        "ref": {
                            "ci_workflow": None,
                            "file": {
                                "connection": "hotel",
                                "path_pattern": "bookings/*.parquet",
                            },
                        },
                        "cadence": cadence(),
                        "grace_minutes": 60,
                        "timezone": "UTC",
                    },
                    data_feed(
                        name="bookings-table-feed",
                        ref={"connection": "hotel", "table": "bookings"},
                    ),
                ],
                "on_call": [],
                "checks": [
                    {
                        "check_id": "chk-drift",
                        "name": "Drift",
                        "category": "drift",
                        "cadence": cadence(),
                        "timezone": "UTC",
                        "results": {
                            "connection": "hotel",
                            "path_pattern": "monitoring/*.jsonl",
                        },
                    }
                ],
                "results_location": {
                    "connection": "hotel",
                    "path_pattern": "monitoring/results/*.jsonl",
                },
            },
        )
    )


def test_map_stages_of_folder_patterns() -> None:
    def with_pattern(pattern: str) -> HandoffPayload:
        return HandoffPayload.model_validate(
            handoff(
                pipeline=[{"name": "bookings", "entry": "e.py", "inputs": [], "outputs": ["b"]}],
                edges=[],
                operations={
                    "scheduler": None,
                    "feeds": [
                        {
                            "name": "feed",
                            "kind": "job",
                            "ref": {
                                "ci_workflow": None,
                                "file": {"connection": "c", "path_pattern": pattern},
                            },
                            "cadence": cadence(),
                            "grace_minutes": 1,
                            "timezone": "UTC",
                        }
                    ],
                    "on_call": [],
                    "checks": [],
                    "results_location": {"connection": "c", "path_pattern": "r/*.jsonl"},
                },
            )
        )

    assert map_stages_of("feed", with_pattern("bookings/*.parquet")) == ["bookings"]
    assert map_stages_of("feed", with_pattern("/bookings/*.parquet")) == ["bookings"]
    assert map_stages_of("feed", with_pattern("bookings/sub/*.parquet")) == ["bookings"]
    assert map_stages_of("feed", with_pattern(r"bookings\sub\*.parquet")) == ["bookings"]
    assert map_stages_of("feed", with_pattern("*.parquet")) == []
    assert map_stages_of("feed", with_pattern("b*okings/*.parquet")) == []
    assert map_stages_of("feed", with_pattern("b?okings/*.parquet")) == []
    assert map_stages_of("feed", with_pattern("b[oo]kings/*.parquet")) == []


def test_map_stages_of_walkthrough_3() -> None:
    p = walkthrough_3_payload()
    # Feed with job file pattern top folder "bookings" matches "bookings" stage
    assert map_stages_of("daily-scoring", p) == ["bookings"]
    # Feed with data table "bookings" matches "bookings" stage
    assert map_stages_of("bookings-table-feed", p) == ["bookings"]
    # Check by check_id with file pattern top folder "monitoring" matches "monitoring" stage
    assert map_stages_of("chk-drift", p) == ["monitoring"]
    # Check by name with file pattern top folder "monitoring" matches "monitoring" stage
    assert map_stages_of("Drift", p) == ["monitoring"]
    # Model with predictions table "predictions" matches "predictions" stage
    assert map_stages_of("hotel-cancellation", p) == ["predictions"]
    # Direct stage input/output matches via stages_of
    assert map_stages_of("bookings", p) == ["bookings"]
    assert map_stages_of("predictions", p) == ["predictions"]
    # Unrelated subject matches nothing
    assert map_stages_of("unrelated_feed_or_check", p) == []


@pytest.mark.asyncio
async def test_walkthrough_3_handoff_gives_no_unknown_stage_when_results_exist() -> None:
    p = walkthrough_3_payload()

    # When no results exist, all stages are unknown
    empty_rig = Rig(current=p, states=[])
    empty_view = await empty_rig.builder.build(PROJECT)
    assert [s.status for s in empty_view.stages] == ["unknown", "unknown", "unknown"]

    # When results exist for the feed, check, and model, no stage is unknown
    rig = Rig(current=p, states=[])
    rig.results.rows = [
        stored("daily-scoring", 1.0, "hotel-cancellation", subject="daily-scoring"),
        stored("chk-drift", 0.05, "hotel-cancellation", subject="chk-drift"),
        stored("eval", 0.85, "hotel-cancellation", subject="hotel-cancellation"),
    ]
    view = await rig.builder.build(PROJECT)
    assert [s.status for s in view.stages] == ["ok", "ok", "ok"]
    assert all(s.status != "unknown" for s in view.stages)


@pytest.mark.asyncio
async def test_walkthrough_3_incident_and_late_signals_map_to_stages() -> None:
    p = walkthrough_3_payload()
    rig = Rig(
        current=p,
        states=[expectation("chk-drift", "late")],
    )
    rig.incidents.add("inc_1", subjects=("daily-scoring",))

    view = await rig.builder.build(PROJECT)
    by_name = {s.name: s for s in view.stages}
    assert by_name["bookings"].status == "incident"
    assert by_name["bookings"].incident_ids == ["inc_1"]
    assert by_name["monitoring"].status == "late"
