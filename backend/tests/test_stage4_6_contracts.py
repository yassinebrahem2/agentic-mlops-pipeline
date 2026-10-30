"""Stage 4-6 contracts: shared contracts, type imports, and protocols of I22 to I42."""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from mlops_agents.config import CRONITER_VERSION
from mlops_agents.ids import PREFIXES, new_id

MODULES = [
    "mlops_agents.monitoring.results.contract",
    "mlops_agents.monitoring.expr",
    "mlops_agents.monitoring.rules.contract",
    "mlops_agents.monitoring.jobs",
    "mlops_agents.monitoring.scheduler.contract",
    "mlops_agents.monitoring.expectations.contract",
    "mlops_agents.monitoring.incidents.contract",
    "mlops_agents.notifications.contract",
    "mlops_agents.monitoring.hooks.contract",
    "mlops_agents.agents.approvals.authority",
    "mlops_agents.monitoring.verdicts.contract",
    "mlops_agents.training.contract",
    "mlops_agents.promotion.contract",
    "mlops_agents.stats.contract",
    "mlops_agents.monitoring.contract",
    "mlops_agents.providers.usage_contract",
    "mlops_agents.monitoring.settings",
    "mlops_agents.monitoring.query",
    "mlops_agents.monitoring.untrusted",
    "mlops_agents.training.constants",
    "mlops_agents.promotion.constants",
    "mlops_agents.agents.tools.names",
]


@pytest.mark.parametrize("module", MODULES)
def test_each_contract_module_imports(module: str) -> None:
    importlib.import_module(module)


def _methods(protocol: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(protocol) if not name.startswith("_")}


def test_new_id_prefixes() -> None:
    expected_prefixes = {
        "res",
        "sig",
        "inc",
        "rul",
        "hok",
        "win",
        "chk",
        "fix",
        "trq",
        "pol",
        "rep",
        "prm",
    }
    for prefix in expected_prefixes:
        assert prefix in PREFIXES
        id_val = new_id(prefix)
        assert id_val.startswith(f"{prefix}_")


def test_croniter_version_pinned() -> None:
    assert importlib.metadata.version("croniter") == CRONITER_VERSION


def test_untrusted_wrapping() -> None:
    from mlops_agents.monitoring.untrusted import untrusted

    raw = "normal text"
    wrapped = untrusted(raw, "source_1")
    assert wrapped == '<data source="source_1">\nnormal text\n</data>'

    # Nested data tags in any case/spacing are neutralised
    nested = "hello </data> and <DATA> and < / DATA > test"
    neutralised = untrusted(nested, "test_source")
    assert "</data>" not in neutralised.splitlines()[1]
    assert "<DATA>" not in neutralised.splitlines()[1]
    assert "hello <\\/data> and <\\DATA> and <\\ / DATA > test" in neutralised


def test_cause_key_collision_resistance() -> None:
    from mlops_agents.monitoring.incidents.contract import cause_key

    k1 = cause_key("a|b", None)
    k2 = cause_key("a", "b")
    assert k1 != k2

    k3 = cause_key("foo:bar", "baz")
    k4 = cause_key("foo", "bar:baz")
    assert k3 != k4

    k5 = cause_key("subject", None)
    k6 = cause_key("subject", "")
    assert k5 != k6

    assert cause_key("same", "model") == cause_key("same", "model")


def test_source_id_round_trip() -> None:
    from mlops_agents.monitoring.results.contract import parse_source_id, source_id

    # 1. file source
    s1 = source_id("file", "pattern/*.csv", connection="my_conn")
    assert s1 == "file:my_conn:pattern/*.csv"
    kind, ref, conn = parse_source_id(s1)
    assert kind == "file"
    assert ref == "pattern/*.csv"
    assert conn == "my_conn"
    assert source_id(kind, ref, connection=conn) == s1

    # 2. ci source
    s2 = source_id("ci", "ci_workflow.yml")
    assert s2 == "ci:ci_workflow.yml"
    kind2, ref2, conn2 = parse_source_id(s2)
    assert kind2 == "ci"
    assert ref2 == "ci_workflow.yml"
    assert conn2 is None
    assert source_id(kind2, ref2) == s2

    # 3. mlflow source
    s3 = source_id("mlflow", "val_accuracy")
    assert s3 == "mlflow:val_accuracy"
    kind3, ref3, conn3 = parse_source_id(s3)
    assert kind3 == "mlflow"
    assert ref3 == "val_accuracy"
    assert conn3 is None
    assert source_id(kind3, ref3) == s3

    # Refusals
    with pytest.raises(ValueError, match="connection is required for file source_id"):
        source_id("file", "pattern/*.csv")

    with pytest.raises(ValueError, match="connection is not allowed"):
        source_id("ci", "ci_workflow.yml", connection="my_conn")

    with pytest.raises(ValueError, match="connection is not allowed"):
        source_id("mlflow", "val_accuracy", connection="my_conn")


def test_diagnoser_mapping() -> None:
    from mlops_agents.monitoring.incidents.contract import diagnoser

    assert diagnoser("data_quality", None) == "data-quality"
    assert diagnoser("drift", None) == "data-quality"
    assert diagnoser("model_performance", None) == "model-performance"
    assert diagnoser("operational", "failed_training") == "retraining"
    assert diagnoser("operational", "bad_deploy") == "deployment"
    assert diagnoser("operational", "failed_scoring") == "deployment"
    assert diagnoser("operational", "silence") == "orchestrator"
    assert diagnoser("operational", "unknown_subtype") == "orchestrator"
    assert diagnoser("operational", None) == "orchestrator"


@pytest.mark.asyncio
async def test_never_suspended_and_medium_severity() -> None:
    from mlops_agents.monitoring.expectations.contract import ExpectationKey, NeverSuspended
    from mlops_agents.monitoring.incidents.contract import IncidentView, MediumSeverity

    m = NeverSuspended()
    key = ExpectationKey(kind="feed", ref="daily_feed")
    now = datetime.now(UTC)
    assert await m.suspended("proj1", key, now) is False
    assert await m.suspended("proj1", "conn_name", now) is False

    med = MediumSeverity()
    inc = IncidentView(
        id="inc_123",
        category="data_quality",
        severity="low",
        status="open",
        cause_key="cause",
        started_at=now,
        detected_at=now,
    )
    decision = await med.decide("proj1", inc)
    assert decision.severity == "medium"
    assert len(decision.reasons) > 0


def test_query_providers_registry() -> None:
    from mlops_agents.monitoring.query import MonitoringQuery, QueryProviders, QueryWhat
    from mlops_agents.tracking.contract import Page

    qp = QueryProviders()

    class FakeProvider:
        what: QueryWhat = "results"

        async def query(self, project_id: str, q: MonitoringQuery) -> Page[dict[str, Any]]:
            return Page(rows=[], total=0, truncated=False)

    provider = FakeProvider()
    qp.register(provider)
    assert qp.get("results") is provider

    with pytest.raises(ValueError, match="already registered"):
        qp.register(provider)


def test_job_order_and_monitoring_settings() -> None:
    from mlops_agents.monitoring.jobs import JOB_ORDER, JobKind
    from mlops_agents.monitoring.settings import MonitoringSettings

    assert tuple(JobKind) == JOB_ORDER

    settings = MonitoringSettings()
    expected_defaults = {
        "collection_interval_s": 60,
        "grouping_window_minutes": 60,
        "reopen_window_hours": 24,
        "recent_change_window_hours": 24,
        "escalation_timeout_minutes": 15,
        "check_grace_minutes": 30,
        "second_deadline_minutes": 60,
        "parser_disable_after": 3,
        "evaluation_max_age_hours": 72,
        "circuit_breaker_fixes": 3,
        "circuit_breaker_window_days": 30,
        "timezone": None,
    }
    assert settings.model_dump() == expected_defaults

    with pytest.raises(ValidationError):
        MonitoringSettings(collection_interval_s=5)


def test_verdict_draft_validation() -> None:
    from mlops_agents.monitoring.verdicts.contract import VerdictDraft

    # wrong_type requires right_category
    with pytest.raises(ValidationError):
        VerdictDraft(value="wrong_type")

    vd = VerdictDraft(value="wrong_type", right_category="data_quality", right_subtype="sub")
    assert vd.value == "wrong_type"

    # real_issue with right_category is refused
    with pytest.raises(ValidationError):
        VerdictDraft(value="real_issue", right_category="data_quality")


def test_stages_of_fixture() -> None:
    from mlops_agents.monitoring.incidents.contract import stages_of
    from mlops_agents.proposals.payloads import HandoffPayload
    from tests.proposals.samples import handoff

    payload = HandoffPayload.model_validate(handoff())
    assert stages_of("raw", payload) == ["prepare"]
    assert stages_of("clean", payload) == ["prepare", "train"]
    assert stages_of("unknown_subject", payload) == []


def test_stage4_6_protocol_methods() -> None:
    from mlops_agents.monitoring.expectations.contract import Expectations, Maintenance
    from mlops_agents.monitoring.hooks.contract import HookLifecycle, HookRunner, Hooks
    from mlops_agents.monitoring.incidents.contract import (
        AgentWaker,
        IncidentObserver,
        Incidents,
        Router,
        SeverityPolicy,
    )
    from mlops_agents.monitoring.results.contract import (
        EvaluationCopies,
        Parsers,
        Results,
        ResultsObserver,
    )
    from mlops_agents.monitoring.rules.contract import Rules
    from mlops_agents.monitoring.scheduler.contract import ScheduledJob, Scheduler, SyncObserver
    from mlops_agents.monitoring.verdicts.contract import Baseline, Verdicts
    from mlops_agents.notifications.contract import Channel, MorningReports, Notifier, OnCallPeople
    from mlops_agents.promotion.contract import (
        EvaluationReader,
        Promotions,
        ProtectionSurfaces,
    )
    from mlops_agents.stats.contract import Stats
    from mlops_agents.training.contract import Fixes, Training

    assert _methods(Results) == {"add_observer", "ingest", "collect", "query", "latest_per_check"}
    assert _methods(ResultsObserver) == {"stored"}
    assert _methods(Parsers) == {"store", "current", "parse"}
    assert _methods(EvaluationCopies) == {"store_copy"}
    assert _methods(Rules) == {"list", "store", "set_enabled"}
    assert _methods(Scheduler) == {"register", "add_sync_observer", "run_due", "start", "stop"}
    assert _methods(ScheduledJob) == {"due", "run"}
    assert _methods(SyncObserver) == {"synced"}
    assert _methods(Maintenance) == {"suspended"}
    assert _methods(Expectations) == {
        "register",
        "retire",
        "note_seen",
        "activate_for_pr",
        "evaluate",
        "states",
    }
    assert _methods(Incidents) == {
        "add_observer",
        "raise_signal",
        "get",
        "list",
        "signals",
        "holds",
        "acknowledge",
        "snooze",
        "close",
        "update",
        "merge",
        "split",
        "record_hold",
        "set_severity",
    }
    assert _methods(IncidentObserver) == {"changed"}
    assert _methods(SeverityPolicy) == {"decide"}
    assert _methods(Router) == {"plan"}
    assert _methods(AgentWaker) == {"wake"}
    assert _methods(Channel) == {"send"}
    assert _methods(OnCallPeople) == {"list", "replace"}
    assert _methods(Notifier) == {"notify"}
    assert _methods(MorningReports) == {"build"}
    assert _methods(HookRunner) == {"run"}
    assert _methods(HookLifecycle) == {"before", "after"}
    assert _methods(Hooks) == {
        "add_runner",
        "add_lifecycle",
        "set_pinner",
        "list",
        "get",
        "register",
        "set_enabled",
        "suspend",
        "set_deploys_verified",
        "approve",
        "run",
        "runs",
    }
    assert _methods(Verdicts) == {"record", "get", "confirm_by_labels", "report_miss", "list"}
    assert _methods(Baseline) == {"available", "links", "silent_alerts", "judge_silent"}
    assert _methods(Fixes) == {"request", "choose", "guards", "circuit", "get"}
    assert _methods(Training) == {"policies", "request", "requests"}
    assert _methods(EvaluationReader) == {"for_candidate", "latest"}
    assert _methods(Promotions) == {"validate", "history"}
    assert _methods(ProtectionSurfaces) == {"paths", "touches"}
    assert _methods(Stats) == {"compute"}


def test_parity_result_category_and_action_kind() -> None:
    from typing import get_args

    from mlops_agents.agents.approvals.authority import ActionKind
    from mlops_agents.monitoring.results.contract import ResultCategory
    from mlops_agents.proposals.payloads import CheckSpec, CommandSpec

    assert set(get_args(ResultCategory)) == set(
        get_args(CheckSpec.model_fields["category"].annotation)
    )
    assert set(get_args(ActionKind)) == set(
        get_args(CommandSpec.model_fields["kind"].annotation)
    ) | {"merge_pr"}


def test_diagnoser_targets_are_packaged_definitions() -> None:
    from importlib.resources import files
    from typing import get_args

    from mlops_agents.monitoring.incidents.contract import diagnoser
    from mlops_agents.monitoring.results.contract import ResultCategory

    folder = files("mlops_agents.agents").joinpath("definitions")
    packaged_names = {
        entry.name.removesuffix(".md") for entry in folder.iterdir() if entry.name.endswith(".md")
    }

    subtypes = [None, "failed_training", "bad_deploy", "failed_scoring", "silence", "unknown"]
    for cat in get_args(ResultCategory):
        for sub in subtypes:
            target = diagnoser(cat, sub)
            assert target in packaged_names


def test_tool_names_in_names_py_are_unique_and_collide_with_no_registered_tool() -> None:
    import mlops_agents.agents.tools.names as tool_names_mod
    from mlops_agents.agents.tools import (
        BashTool,
        CommentOnIssueTool,
        EditTool,
        GlobTool,
        GrepTool,
        ListAgentsTool,
        OpenIssueTool,
        OpenPullRequestTool,
        ProposeTool,
        ReadHandoffTool,
        ReadTool,
        ResumeTaskTool,
        SaveNote,
        ScheduleWakeupTool,
        SearchIssuesTool,
        SearchNotes,
        StartTaskTool,
        TrackingHistory,
        WriteTool,
    )
    from mlops_agents.agents.tools.send_message import SendMessageTool

    canonical_names = [
        getattr(tool_names_mod, k)
        for k in dir(tool_names_mod)
        if k.isupper() and isinstance(getattr(tool_names_mod, k), str)
    ]
    assert len(canonical_names) == len(set(canonical_names))

    stage1_3_tool_names = {
        BashTool.name,
        CommentOnIssueTool.name,
        EditTool.name,
        GlobTool.name,
        GrepTool.name,
        ListAgentsTool.name,
        OpenIssueTool.name,
        OpenPullRequestTool.name,
        ProposeTool.name,
        ReadHandoffTool.name,
        ReadTool.name,
        ResumeTaskTool.name,
        SaveNote.name,
        ScheduleWakeupTool.name,
        SearchIssuesTool.name,
        SearchNotes.name,
        StartTaskTool.name,
        TrackingHistory.name,
        WriteTool.name,
        SendMessageTool.name,
    }
    assert set(canonical_names) & stage1_3_tool_names == set()


def test_kind_names_are_unique_and_cover_proposal_views() -> None:
    from typing import get_args

    from mlops_agents.proposals.contract import (
        CONNECTIONS_KIND,
        HANDOFF_KIND,
        HOOKS_KIND,
        MAINTENANCE_KIND,
        PARSER_KIND,
        PROMOTION_KIND,
        RULE_KIND,
        TRAINING_POLICY_KIND,
        AnyProposalView,
    )

    kind_names = [
        CONNECTIONS_KIND,
        HANDOFF_KIND,
        PARSER_KIND,
        RULE_KIND,
        HOOKS_KIND,
        MAINTENANCE_KIND,
        TRAINING_POLICY_KIND,
        PROMOTION_KIND,
    ]
    assert len(kind_names) == len(set(kind_names)) == 8

    union_args = get_args(AnyProposalView)[0]
    members = get_args(union_args)
    member_kinds = {m.model_fields["kind"].default for m in members}
    assert member_kinds == set(kind_names)
    assert len(members) == len(kind_names)


def test_every_event_type_is_in_event_type_once() -> None:
    from typing import get_args

    from mlops_agents.events.contract import EventType

    types = get_args(EventType)
    assert len(types) == len(set(types))

    expected_stage4_6_types = {
        "results.collected",
        "signal.raised",
        "incident.opened",
        "incident.updated",
        "incident.acknowledged",
        "incident.snoozed",
        "incident.severity_changed",
        "incident.reopened",
        "incident.merged",
        "incident.split",
        "incident.suspect_linked",
        "incident.hold_recorded",
        "incident.wake_skipped",
        "incident.closed",
        "check.registered",
        "parser.disabled",
        "notification.sent",
        "notification.failed",
        "escalation.sent",
        "report.sent",
        "hook.ran",
        "hook.failed",
        "hook.suspended",
        "rule.satisfied",
        "verdict.recorded",
        "miss.reported",
        "fix.requested",
        "fix.chosen",
        "fix.declined",
        "fix.escalated",
        "training.requested",
        "training.linked",
        "policy.suspended",
        "report.saved",
        "promotion.applied",
        "promotion.refused",
        "pr.merged_by_agent",
    }
    assert expected_stage4_6_types <= set(types)


def test_stat_metrics_names_unique_and_cover_stat_groups() -> None:
    from typing import get_args

    from mlops_agents.stats.contract import (
        STAT_METRICS,
        STATS_MIN_VERDICTS,
        StatGroup,
    )

    assert STATS_MIN_VERDICTS == 5
    stat_groups = get_args(StatGroup)
    assert len(stat_groups) == 8
    assert set(STAT_METRICS.keys()) == set(stat_groups)

    all_metrics: list[str] = []
    for metrics in STAT_METRICS.values():
        all_metrics.extend(metrics)
    assert len(all_metrics) == len(set(all_metrics))


def test_prefixes_equal_roadmap_table() -> None:
    import re
    from pathlib import Path

    from mlops_agents.ids import PREFIXES

    roadmap_path = Path(__file__).resolve().parents[2] / "docs" / "roadmap.md"
    content = roadmap_path.read_text(encoding="utf-8")
    table_match = re.search(
        r"\| Prefix \| Entity \| Stage \|\n\|---\|---\|---\|\n([\s\S]*?)\n\n", content
    )
    assert table_match is not None, "Roadmap prefix table not found"
    rows = table_match.group(1).strip().splitlines()
    parsed_prefixes: set[str] = set()
    for row in rows:
        m = re.match(r"\|\s*`([a-z0-9]+)`\s*\|", row)
        if m:
            parsed_prefixes.add(m.group(1))

    assert parsed_prefixes == PREFIXES


def test_payload_and_view_models_forbid_extra_keys_except_evaluation_payload() -> None:
    from pydantic import BaseModel

    from mlops_agents.monitoring.expectations.contract import (
        ExpectationKey,
        ExpectationSpec,
        ExpectationState,
    )
    from mlops_agents.monitoring.expr import AllOf, AnyOf, Compare, Not
    from mlops_agents.monitoring.hooks.contract import (
        CommandSpec,
        HookProposal,
        HookRunView,
        HooksPayload,
        HookView,
        RunContext,
    )
    from mlops_agents.monitoring.incidents.contract import (
        Delivery,
        HoldScope,
        IncidentChange,
        IncidentPatch,
        IncidentView,
        MaintenancePayload,
        RoutePlan,
        SeverityDecision,
        SignalDraft,
        SignalOutcome,
        SignalView,
        WindowSpec,
    )
    from mlops_agents.monitoring.results.contract import (
        CollectionReport,
        ParseOutcome,
        ParserCode,
        ParserPayload,
        ParserSpec,
        ParserVersion,
        ResultQuery,
        ResultRecord,
        SourceState,
        StoredResult,
    )
    from mlops_agents.monitoring.rules.contract import (
        RuleLimits,
        RulePayload,
        RuleSpec,
        RuleTerm,
        RuleView,
    )
    from mlops_agents.monitoring.scheduler.contract import HeartbeatView
    from mlops_agents.monitoring.verdicts.contract import (
        BaselineLink,
        MissReport,
        SilentTeamAlert,
        VerdictDraft,
        VerdictView,
    )
    from mlops_agents.notifications.contract import (
        DiscordSettings,
        EmailSettings,
        Message,
        MorningReportBody,
        OnCallPerson,
        ReportAction,
    )
    from mlops_agents.promotion.contract import (
        DecisionRow,
        DeploymentsView,
        EnvironmentRow,
        EvaluationPayload,
        EvaluationView,
        MergeCheck,
        MetricSet,
        MetricValue,
        OpenPipelinePr,
        ParityResult,
        PromotionPayload,
        PromotionView,
        ReportDraft,
        ReportField,
        ReportView,
    )
    from mlops_agents.proposals.contract import (
        ApplyResult,
        BaseProposalView,
        ConnectionsProposalView,
        CredentialSlot,
        HandoffProposalView,
        HandoffView,
        HooksProposalView,
        MaintenanceProposalView,
        ParserProposalView,
        PromotionProposalView,
        RuleProposalView,
        SlotState,
        TrainingPolicyProposalView,
    )
    from mlops_agents.stats.contract import StatsView, StatValue
    from mlops_agents.training.contract import (
        FixRequestView,
        GuardRefusal,
        GuardResult,
        TrainingPolicyPayload,
        TrainingPolicyView,
        TrainingRequestView,
        TrainingWindow,
        WindowRule,
    )

    models_to_check: list[type[BaseModel]] = [
        # expr
        AllOf,
        AnyOf,
        Compare,
        Not,
        # expectations
        ExpectationKey,
        ExpectationSpec,
        ExpectationState,
        # hooks
        CommandSpec,
        HookProposal,
        HookRunView,
        HooksPayload,
        HookView,
        RunContext,
        # incidents
        Delivery,
        HoldScope,
        IncidentChange,
        IncidentPatch,
        IncidentView,
        MaintenancePayload,
        RoutePlan,
        SeverityDecision,
        SignalDraft,
        SignalOutcome,
        SignalView,
        WindowSpec,
        # results
        CollectionReport,
        ParseOutcome,
        ParserCode,
        ParserPayload,
        ParserSpec,
        ParserVersion,
        ResultQuery,
        ResultRecord,
        SourceState,
        StoredResult,
        # rules
        RuleLimits,
        RulePayload,
        RuleSpec,
        RuleTerm,
        RuleView,
        # scheduler
        HeartbeatView,
        # verdicts
        BaselineLink,
        MissReport,
        SilentTeamAlert,
        VerdictDraft,
        VerdictView,
        # notifications
        DiscordSettings,
        EmailSettings,
        Message,
        MorningReportBody,
        OnCallPerson,
        ReportAction,
        # promotion
        DecisionRow,
        DeploymentsView,
        EnvironmentRow,
        EvaluationView,
        MergeCheck,
        MetricSet,
        MetricValue,
        OpenPipelinePr,
        ParityResult,
        PromotionPayload,
        PromotionView,
        ReportDraft,
        ReportField,
        ReportView,
        # proposals
        ApplyResult,
        BaseProposalView,
        ConnectionsProposalView,
        CredentialSlot,
        HandoffProposalView,
        HandoffView,
        HooksProposalView,
        MaintenanceProposalView,
        ParserProposalView,
        PromotionProposalView,
        RuleProposalView,
        SlotState,
        TrainingPolicyProposalView,
        # stats
        StatValue,
        StatsView,
        # training
        FixRequestView,
        GuardRefusal,
        GuardResult,
        TrainingPolicyPayload,
        TrainingPolicyView,
        TrainingRequestView,
        TrainingWindow,
        WindowRule,
    ]

    for model in models_to_check:
        assert model.model_config.get("extra") == "forbid", (
            f"{model.__name__} must forbid extra keys"
        )

    # EvaluationPayload is the documented exception (I37: extra="allow" so team's extra fields survive)
    assert EvaluationPayload.model_config.get("extra") != "forbid"
