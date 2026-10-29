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
    assert settings.collection_interval_s == 60
    assert settings.grouping_window_minutes == 60
    assert settings.reopen_window_hours == 24
    assert settings.timezone is None

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
