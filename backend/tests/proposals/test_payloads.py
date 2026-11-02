"""The I19 payload models: shapes, one-of rules and the union a ProposalView carries."""

import json
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.proposals.contract import ProposalView
from mlops_agents.proposals.payloads import (
    Cadence,
    ConnectionsPayload,
    ConnectionSpec,
    GrantableLevel,
    HandoffPayload,
    JobFeedRef,
    ModelSpec,
    OnCall,
    Placeholder,
    TrackingSourceSpec,
)
from tests.proposals.samples import cadence, connections, error_text, handoff, model


def test_full_handoff_round_trips_through_json() -> None:
    payload = HandoffPayload.model_validate(handoff())
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
            handoff(grants={"network": False, "permission_level": "bypass"})
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
    handoff_view = ProposalView.model_validate({**common, "kind": "handoff", "payload": handoff()})
    assert isinstance(handoff_view.payload, HandoffPayload)
    connections_view = ProposalView.model_validate(
        {**common, "kind": "connections", "payload": connections()}
    )
    assert isinstance(connections_view.payload, ConnectionsPayload)
    with pytest.raises(ValidationError):
        ProposalView.model_validate({**common, "kind": "x", "payload": {"unrelated": 1}})
    round_trip = ProposalView.model_validate_json(handoff_view.model_dump_json())
    assert isinstance(round_trip.payload, HandoffPayload)
    round_trip = ProposalView.model_validate_json(connections_view.model_dump_json())
    assert isinstance(round_trip.payload, ConnectionsPayload)


def test_proposal_view_without_approval_keeps_none() -> None:
    view = ProposalView.model_validate(
        {
            "id": "prp_1",
            "kind": "connections",
            "key": "connections",
            "payload": connections(),
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
    for cls, good in (
        (ConnectionsPayload, connections()),
        (HandoffPayload, handoff()),
        (ModelSpec, model()),
        (Cadence, cadence()),
    ):
        model_cls: type[BaseModel] = cls
        model_cls.model_validate(good)
        with pytest.raises(ValidationError):
            model_cls.model_validate({**good, "extra_field": 1})
    nested = handoff()
    nested["operations"]["feeds"][0]["surprise"] = True
    with pytest.raises(ValidationError):
        HandoffPayload.model_validate(nested)


@pytest.mark.parametrize("name", ["hotel data", "Hotel", "-hotel", "a" * 41, ""])
def test_connection_name_must_follow_the_pattern(name: str) -> None:
    spec = connections()["connections"][0]
    with pytest.raises(ValidationError):
        ConnectionSpec.model_validate({**spec, "name": name})
    assert ConnectionSpec.model_validate(spec).name == "hotel-data"


@pytest.mark.parametrize("kind", ["s3", "Local_Folder", ""])
def test_connection_kind_is_local_folder_only(kind: str) -> None:
    spec = connections()["connections"][0]
    with pytest.raises(ValidationError):
        ConnectionSpec.model_validate({**spec, "kind": kind})


def test_connection_settings_carry_no_extra_keys() -> None:
    spec = connections()["connections"][0]
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
    assert "github_actions" in error_text(err)
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
    on_call = handoff()["operations"]["on_call"][0]
    OnCall.model_validate(on_call)
    with pytest.raises(ValidationError):
        OnCall.model_validate({**on_call, "contacts": []})


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


def test_handoff_with_check_without_cadence_validates() -> None:
    data = handoff()
    check_no_cadence = {
        "check_id": "chk-on-demand",
        "name": "Manual evaluation",
        "category": "model_performance",
        "timezone": "UTC",
        "results": {"metric": "accuracy", "threshold": 0.9, "fires_when": "below"},
    }
    data["operations"]["checks"].append(check_no_cadence)
    payload = HandoffPayload.model_validate(data)
    assert payload.operations.checks[-1].cadence is None

    # Stored handoff with cadence loads as before
    stored_payload = HandoffPayload.model_validate(handoff())
    for check in stored_payload.operations.checks:
        assert check.cadence is not None
