"""The I19 payload models: shapes, one-of rules, validators and cross-references."""

import json
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.proposals.contract import ProposalView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    CommandSpec,
    ConnectionsPayload,
    ConnectionSpec,
    GrantableLevel,
    HandoffPayload,
    JobFeedRef,
    ModelSpec,
    OnCall,
    OnCallHours,
    Placeholder,
    TrackingSourceSpec,
)


def _profile() -> dict[str, Any]:
    return {
        "objective": "Predict cancellations",
        "problem_type": "binary_classification",
        "target_column": "canceled",
        "positive_class": "1",
        "primary_metric": "recall",
        "metric_rationale": "Missing a cancellation costs more than a false alarm",
        "operating_threshold": 0.4,
        "label_delay_days": 7,
        "label_maturity_days": 14,
        "serving_mode": "batch",
        "error_costs": "A miss costs ten times a false alarm",
        "cost_ratio": 10.0,
        "groups": [{"column": "country", "values": None}],
    }


def _command(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "train",
        "type": "command",
        "template": "python train.py --model {model}",
        "workflow": None,
        "inputs": {},
        "placeholders": ["model"],
        "deploys": False,
        "entry_files": ["train.py"],
    }
    return {**base, **overrides}


def _model(name: str = "cancel", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "profile": _profile(),
        "predictions": {"connection": "hotel", "table": "predictions"},
        "labels": {"connection": "hotel", "table": "labels"},
        "commands": [_command()],
        "promotion": "team_gate",
        "environments": {"production": "production", "staging": "staging"},
        "production_signal": {"kind": "tracker_alias", "ref": "production"},
    }
    return {**base, **overrides}


def _cadence() -> dict[str, Any]:
    return {"cron": "0 6 * * *", "every_minutes": None}


def _handoff(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "models": [_model("cancel"), _model("noshow")],
        "commands": [
            _command(
                kind="rerun",
                type="ci_dispatch",
                template=None,
                workflow="rerun.yml",
                inputs={"day": "{window_end}"},
                placeholders=["window_end"],
            )
        ],
        "data_roles": [
            {
                "connection": "hotel",
                "table": "predictions",
                "role": "predictions",
                "id_column": "booking_id",
                "time_column": "scored_at",
            },
            {
                "connection": "hotel",
                "table": "labels",
                "role": "labels",
                "id_column": "booking_id",
                "time_column": None,
            },
        ],
        "tracking": [
            {"type": "mlflow", "settings": {"url": "http://mlflow:5000"}, "auth": "none"},
            {
                "type": "github_actions",
                "settings": {"repository": "acme/ml", "workflows": ["train.yml"]},
                "auth": "none",
            },
        ],
        "pipeline": [
            {"name": "prepare", "entry": "prepare.py", "inputs": ["raw"], "outputs": ["clean"]},
            {"name": "train", "entry": "train.py", "inputs": ["clean"], "outputs": ["model"]},
        ],
        "edges": [{"from_stage": "prepare", "to_stage": "train"}],
        "operations": {
            "scheduler": {"name": "github_actions", "config_location": ".github/workflows"},
            "feeds": [
                {
                    "name": "bookings",
                    "kind": "data",
                    "ref": {"connection": "hotel", "table": "bookings"},
                    "cadence": _cadence(),
                    "grace_minutes": 60,
                    "timezone": "Europe/Paris",
                },
                {
                    "name": "nightly",
                    "kind": "job",
                    "ref": {"ci_workflow": "nightly.yml", "file": None},
                    "cadence": {"cron": None, "every_minutes": 1440},
                    "grace_minutes": 30,
                    "timezone": "UTC",
                },
            ],
            "on_call": [
                {
                    "name": "ml-team",
                    "contacts": [
                        {"channel": "discord", "address": "#ml-alerts"},
                        {"channel": "email", "address": "ml@acme.test"},
                    ],
                    "hours": {
                        "days": ["mon", "tue", "wed", "thu", "fri"],
                        "start": "09:00",
                        "end": "17:00",
                    },
                    "timezone": "Europe/Paris",
                    "escalation_order": 1,
                }
            ],
            "checks": [
                {
                    "check_id": "chk-file",
                    "name": "Data quality",
                    "category": "data_quality",
                    "cadence": _cadence(),
                    "timezone": "UTC",
                    "results": {"connection": "hotel", "path_pattern": "dq/*.json"},
                },
                {
                    "check_id": "chk-ci",
                    "name": "Drift",
                    "category": "drift",
                    "cadence": _cadence(),
                    "timezone": "UTC",
                    "results": {"workflow": "drift.yml"},
                },
                {
                    "check_id": "chk-mlflow",
                    "name": "Recall",
                    "category": "model_performance",
                    "cadence": _cadence(),
                    "timezone": "UTC",
                    "results": {"metric": "recall", "threshold": 0.8, "fires_when": "below"},
                },
            ],
            "results_location": {"connection": "hotel", "path_pattern": "results/*.json"},
        },
        "gaps": [{"what": "No drift check", "next_step": "Add one"}],
        "grants": {"network": False, "permission_level": "ask"},
    }
    return {**base, **overrides}


def _connections() -> dict[str, Any]:
    return {
        "connections": [
            {
                "kind": "local_folder",
                "name": "hotel-data",
                "settings": {"path": "/data/hotel"},
                "why": "The booking exports",
            }
        ]
    }


def _error_text(err: pytest.ExceptionInfo[ValidationError]) -> str:
    return " ".join(e["msg"] for e in err.value.errors())


def test_full_handoff_round_trips_through_json() -> None:
    payload = HandoffPayload.model_validate(_handoff())
    again = HandoffPayload.model_validate_json(payload.model_dump_json())
    assert again == payload
    assert json.loads(payload.model_dump_json())["models"][1]["name"] == "noshow"
    feeds = payload.operations.feeds
    assert {type(f.ref).__name__ for f in feeds} == {"DataFeedRef", "JobFeedRef"}
    assert len(payload.operations.on_call[0].contacts) == 2
    assert {type(c.results).__name__ for c in payload.operations.checks} == {
        "FileResults",
        "CiResults",
        "MlflowResults",
    }
    assert {c.type for c in payload.commands} == {"ci_dispatch"}
    assert {c.type for m in payload.models for c in m.commands} == {"command"}


def test_bypass_is_never_grantable() -> None:
    with pytest.raises(ValidationError):
        HandoffPayload.model_validate(
            _handoff(grants={"network": False, "permission_level": "bypass"})
        )


def test_grantable_level_is_permission_level_minus_bypass() -> None:
    assert set(get_args(GrantableLevel)) == set(get_args(PermissionLevel)) - {"bypass"}


def test_proposal_view_picks_the_right_union_member() -> None:
    common: dict[str, Any] = {
        "id": "prp_1",
        "key": "handoff",
        "warnings": [],
        "status": "pending",
        "agent": "onboarding",
        "approval_id": None,
        "turn_id": "trn_1",
        "tool_call_id": "call_1",
        "result": None,
        "slots": [],
        "created": "2026-10-28T00:00:00Z",
    }
    handoff = ProposalView.model_validate({**common, "kind": "handoff", "payload": _handoff()})
    assert isinstance(handoff.payload, HandoffPayload)
    connections = ProposalView.model_validate(
        {**common, "kind": "connections", "payload": _connections()}
    )
    assert isinstance(connections.payload, ConnectionsPayload)
    with pytest.raises(ValidationError):
        ProposalView.model_validate({**common, "kind": "x", "payload": {"unrelated": 1}})
    round_trip = ProposalView.model_validate_json(handoff.model_dump_json())
    assert isinstance(round_trip.payload, HandoffPayload)
    round_trip = ProposalView.model_validate_json(connections.model_dump_json())
    assert isinstance(round_trip.payload, ConnectionsPayload)


def test_proposal_view_without_approval_keeps_none() -> None:
    view = ProposalView.model_validate(
        {
            "id": "prp_1",
            "kind": "connections",
            "key": "connections",
            "payload": _connections(),
            "warnings": ["careful"],
            "status": "pending",
            "agent": "onboarding",
            "approval_id": None,
            "turn_id": "trn_1",
            "tool_call_id": "call_1",
            "result": None,
            "slots": [],
            "created": "2026-10-28T00:00:00Z",
        }
    )
    assert view.approval_id is None
    assert view.warnings == ["careful"]


def test_unknown_fields_are_rejected_at_every_level() -> None:
    for model, good in (
        (ConnectionsPayload, _connections()),
        (HandoffPayload, _handoff()),
        (ModelSpec, _model()),
        (Cadence, _cadence()),
    ):
        model_cls: type[BaseModel] = model
        model_cls.model_validate(good)
        with pytest.raises(ValidationError):
            model_cls.model_validate({**good, "extra_field": 1})
    nested = _handoff()
    nested["operations"]["feeds"][0]["surprise"] = True
    with pytest.raises(ValidationError):
        HandoffPayload.model_validate(nested)


@pytest.mark.parametrize("name", ["hotel data", "Hotel", "-hotel", "a" * 41, ""])
def test_connection_name_must_follow_the_pattern(name: str) -> None:
    spec = _connections()["connections"][0]
    with pytest.raises(ValidationError):
        ConnectionSpec.model_validate({**spec, "name": name})
    assert ConnectionSpec.model_validate(spec).name == "hotel-data"


@pytest.mark.parametrize("kind", ["s3", "Local_Folder", ""])
def test_connection_kind_is_local_folder_only(kind: str) -> None:
    spec = _connections()["connections"][0]
    with pytest.raises(ValidationError):
        ConnectionSpec.model_validate({**spec, "kind": kind})


def test_connection_settings_carry_no_extra_keys() -> None:
    spec = _connections()["connections"][0]
    with pytest.raises(ValidationError):
        ConnectionSpec.model_validate({**spec, "settings": {"path": "/d", "password": "x"}})


def test_tracking_type_must_match_its_settings() -> None:
    mlflow = {"url": "http://mlflow:5000"}
    github: dict[str, Any] = {"repository": "a/b", "workflows": []}
    TrackingSourceSpec.model_validate({"type": "mlflow", "settings": mlflow, "auth": "none"})
    TrackingSourceSpec.model_validate(
        {"type": "github_actions", "settings": github, "auth": "none"}
    )
    with pytest.raises(ValidationError) as err:
        TrackingSourceSpec.model_validate(
            {"type": "github_actions", "settings": mlflow, "auth": "none"}
        )
    assert "github_actions" in _error_text(err)
    with pytest.raises(ValidationError):
        TrackingSourceSpec.model_validate({"type": "mlflow", "settings": github, "auth": "none"})
    with pytest.raises(ValidationError):
        TrackingSourceSpec.model_validate(
            {"type": "mlflow", "settings": {**mlflow, "x": 1}, "auth": "none"}
        )


def test_mlflow_url_with_userinfo_is_rejected_in_a_proposal() -> None:
    with pytest.raises(ValidationError):
        TrackingSourceSpec.model_validate(
            {"type": "mlflow", "settings": {"url": "http://u:pw@host"}, "auth": "none"}
        )


@pytest.mark.parametrize(
    ("ci_workflow", "file"),
    [
        ("a.yml", {"connection": "c", "path_pattern": "*.json"}),
        (None, None),
    ],
)
def test_job_feed_ref_needs_exactly_one(
    ci_workflow: str | None, file: dict[str, Any] | None
) -> None:
    with pytest.raises(ValidationError):
        JobFeedRef.model_validate({"ci_workflow": ci_workflow, "file": file})
    JobFeedRef.model_validate({"ci_workflow": "a.yml", "file": None})
    JobFeedRef.model_validate(
        {"ci_workflow": None, "file": {"connection": "c", "path_pattern": "x"}}
    )


def test_on_call_needs_a_contact() -> None:
    on_call = _handoff()["operations"]["on_call"][0]
    OnCall.model_validate(on_call)
    with pytest.raises(ValidationError):
        OnCall.model_validate({**on_call, "contacts": []})


@pytest.mark.parametrize("zone", ["Europe/Paris", "UTC", "America/Argentina/Buenos_Aires"])
def test_valid_timezones_pass(zone: str) -> None:
    on_call = _handoff()["operations"]["on_call"][0]
    assert OnCall.model_validate({**on_call, "timezone": zone}).timezone == zone


@pytest.mark.parametrize("zone", ["Mars/Base", "", "../etc/passwd", "Paris", "Europe", "zone.tab"])
def test_invalid_timezones_fail_on_every_carrier(zone: str) -> None:
    ops = _handoff()["operations"]
    with pytest.raises(ValidationError):
        OnCall.model_validate({**ops["on_call"][0], "timezone": zone})
    with pytest.raises(ValidationError):
        CheckSpec.model_validate({**ops["checks"][0], "timezone": zone})
    with pytest.raises(ValidationError):
        HandoffPayload.model_validate(
            _handoff(operations={**ops, "feeds": [{**ops["feeds"][0], "timezone": zone}]})
        )


def test_the_timezone_error_names_the_zone() -> None:
    on_call = _handoff()["operations"]["on_call"][0]
    with pytest.raises(ValidationError) as err:
        OnCall.model_validate({**on_call, "timezone": "Mars/Base"})
    assert "Mars/Base" in _error_text(err)


@pytest.mark.parametrize(("start", "end"), [("09:00", "17:00"), ("22:00", "06:00")])
def test_on_call_hours_allow_a_window_over_midnight(start: str, end: str) -> None:
    OnCallHours.model_validate({"days": ["mon"], "start": start, "end": end})


@pytest.mark.parametrize(
    ("start", "end"),
    [("09:00", "09:00"), ("9:00", "17:00"), ("09:00", "24:00"), ("09:60", "17:00"), ("", "17:00")],
)
def test_on_call_hours_reject_equal_or_malformed(start: str, end: str) -> None:
    with pytest.raises(ValidationError):
        OnCallHours.model_validate({"days": ["mon"], "start": start, "end": end})


@pytest.mark.parametrize(
    "fields",
    [
        {"cron": "0 6 * * *", "every_minutes": None},
        {"cron": None, "every_minutes": 15},
    ],
)
def test_cadence_with_exactly_one_field_passes(fields: dict[str, Any]) -> None:
    Cadence.model_validate(fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"cron": "0 6 * * *", "every_minutes": 15},
        {"cron": None, "every_minutes": None},
        {"cron": "0 6 * *", "every_minutes": None},
        {"cron": "0 6 * * * *", "every_minutes": None},
        {"cron": "", "every_minutes": None},
        {"cron": None, "every_minutes": 0},
    ],
)
def test_cadence_rejects_bad_shapes(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Cadence.model_validate(fields)


def test_command_type_rules() -> None:
    CommandSpec.model_validate(_command())
    CommandSpec.model_validate(
        _command(type="ci_dispatch", template=None, workflow="train.yml", placeholders=[])
    )
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(_command(template=None))
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(_command(workflow="train.yml"))
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(_command(type="ci_dispatch", template=None, workflow=None))


def test_template_placeholders_must_be_declared() -> None:
    with pytest.raises(ValidationError) as err:
        CommandSpec.model_validate(
            _command(template="run --end {window_end}", placeholders=["model"])
        )
    assert "window_end" in _error_text(err)
    # Declared but unused is allowed: ci_dispatch inputs use them.
    CommandSpec.model_validate(_command(placeholders=["model", "threshold"]))
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(_command(template="run {unknown}", placeholders=["model"]))
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(_command(template="run {", placeholders=["model"]))


def test_table_use_must_match_a_data_role() -> None:
    bad = _model(predictions={"connection": "hotel", "table": "nope"})
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(_handoff(models=[bad]))
    assert "nope" in _error_text(err)
    with pytest.raises(ValidationError) as err2:
        HandoffPayload.model_validate(
            _handoff(models=[_model(labels={"connection": "other", "table": "labels"})])
        )
    assert "other" in _error_text(err2)
    HandoffPayload.model_validate(_handoff(models=[_model(predictions=None, labels=None)]))


def test_edges_must_join_known_stages() -> None:
    bad = _handoff(edges=[{"from_stage": "prepare", "to_stage": "ship"}])
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(bad)
    assert "ship" in _error_text(err)
    bad_from = _handoff(edges=[{"from_stage": "ghost", "to_stage": "train"}])
    with pytest.raises(ValidationError) as err2:
        HandoffPayload.model_validate(bad_from)
    assert "ghost" in _error_text(err2)


def test_tracker_alias_signal_must_be_an_environment_alias() -> None:
    bad = _model(production_signal={"kind": "tracker_alias", "ref": "champion"})
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(_handoff(models=[bad]))
    assert "champion" in _error_text(err)
    for kind in ("ci_job", "file"):
        other = _model(production_signal={"kind": kind, "ref": "anything"})
        HandoffPayload.model_validate(_handoff(models=[other]))
    HandoffPayload.model_validate(_handoff(models=[_model(production_signal=None)]))


def test_model_names_are_unique() -> None:
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(_handoff(models=[_model("dup"), _model("dup")]))
    assert "dup" in _error_text(err)


def test_placeholder_names_are_the_overviews() -> None:
    assert get_args(Placeholder) == (
        "request_id",
        "model",
        "candidate_version",
        "champion_version",
        "window_start",
        "window_end",
        "mode",
        "threshold",
    )
