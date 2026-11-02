"""The one renderer of a handoff payload, shared by the kickoff and the handoff kind's preview."""

from __future__ import annotations

from typing import Any

from mlops_agents.onboarding.handoff_preview import render, sections
from mlops_agents.proposals.payloads import HandoffPayload
from tests.proposals.samples import command, handoff, job_feed, model

FIXED_ORDER = [
    "models",
    "commands",
    "data_roles",
    "tracking",
    "pipeline",
    "operations",
    "gaps",
    "grants",
]


def _full_dict(**overrides: Any) -> dict[str, Any]:
    models = [
        model("cancel"),
        model(
            "noshow",
            promotion="person",
            predictions=None,
            environments={},
            production_signal={"kind": "file", "ref": "signals/noshow_deployed.txt"},
            commands=[
                command(
                    kind="deploy",
                    template="./deploy.sh {candidate_version}",
                    placeholders=["candidate_version"],
                    deploys=True,
                    entry_files=["deploy.sh"],
                )
            ],
        ),
    ]
    feeds = [
        {
            "name": "bookings",
            "kind": "data",
            "ref": {"connection": "hotel", "table": "bookings"},
            "cadence": {"cron": "30 5 * * 1-5", "every_minutes": None},
            "grace_minutes": 45,
            "timezone": "America/New_York",
        },
        job_feed(),
        job_feed(
            name="scores",
            ref={"ci_workflow": None, "file": {"connection": "hotel", "path_pattern": "s/*.csv"}},
        ),
    ]
    base = handoff(models=models, commands=[command(kind="hold", template="./hold.sh")])
    base["operations"]["feeds"] = feeds
    return {**base, **overrides}


def _full_payload(**overrides: Any) -> HandoffPayload:
    return HandoffPayload.model_validate(_full_dict(**overrides))


def test_sections_are_in_the_fixed_order() -> None:
    assert list(sections(_full_payload())) == FIXED_ORDER


def test_render_is_the_sections_joined_in_order() -> None:
    payload = _full_payload()
    assert render(payload) == "\n\n".join(sections(payload)[key] for key in FIXED_ORDER)


def test_models_section_shows_each_model_in_full() -> None:
    text = sections(_full_payload())["models"]
    for expected in (
        "cancel",
        "noshow",
        "team_gate",
        "person",
        "Predict cancellations",  # the profile's objective
        "recall",
        "label delay: 7 days",
        "hotel.labels",
        "production -> production",  # an environment: tracker alias -> environment
        "staging -> staging",
        "tracker_alias production",  # the first model's production signal
        "file signals/noshow_deployed.txt",
        "{model}",  # a template with its placeholder
        "candidate_version",
        "deploy.sh",
        "deploys: yes",
    ):
        assert expected in text, expected


def test_project_commands_section_lists_project_level_commands() -> None:
    assert "./hold.sh" in sections(_full_payload())["commands"]


def test_data_roles_and_tracking_sections() -> None:
    parts = sections(_full_payload())
    assert "hotel.predictions" in parts["data_roles"]
    assert "booking_id" in parts["data_roles"]
    assert "http://mlflow:5000" in parts["tracking"]
    assert "acme/ml" in parts["tracking"]
    assert "train.yml" in parts["tracking"]


def test_pipeline_section_shows_stages_and_edges() -> None:
    text = sections(_full_payload())["pipeline"]
    assert "prepare.py" in text
    assert "prepare -> train" in text


def test_operations_section_shows_scheduler_feeds_on_call_checks_and_results() -> None:
    text = sections(_full_payload())["operations"]
    for expected in (
        ".github/workflows",  # the scheduler's configuration
        "30 5 * * 1-5",  # a feed's cron
        "America/New_York",  # and its time zone
        "every 1440 minutes",  # an interval feed
        "grace 45 minutes",
        "hotel.bookings",  # a data feed's table
        "nightly.yml",  # a job feed's workflow
        "hotel:s/*.csv",  # a job feed's results file
        "09:00-17:00",  # the on-call hours
        "mon, tue, wed, thu, fri",
        "discord #ml-alerts",  # both contacts
        "email ml@acme.test",
        "Europe/Paris",
        "escalation order 1",
        "0.8",  # an MLflow check's threshold
        "below",  # and its direction
        "hotel:dq/*.json",  # a check's results location
        "drift.yml",
        "hotel:results/*.json",  # the results location
    ):
        assert expected in text, expected


def test_gaps_and_grants_sections() -> None:
    parts = sections(_full_payload())
    assert "No drift check" in parts["gaps"]
    assert "Add one" in parts["gaps"]
    assert "network: off" in parts["grants"]
    assert "permission level: ask" in parts["grants"]


def test_a_missing_optional_value_renders_not_set() -> None:
    data = _full_dict()
    data["operations"]["scheduler"] = None
    data["operations"]["checks"][2]["results"]["threshold"] = None
    parts = sections(HandoffPayload.model_validate(data))
    # The second model has no predictions table; the labels role has no time column.
    assert "predictions: not set" in parts["models"]
    assert "time column not set" in parts["data_roles"]
    assert "scheduler: not set" in parts["operations"]
    assert "threshold not set" in parts["operations"]


def test_an_empty_part_renders_none_never_nothing() -> None:
    parts = sections(_full_payload(gaps=[], edges=[], commands=[]))
    assert "none" in parts["gaps"]
    assert "none" in parts["commands"]
    assert "edges: none" in parts["pipeline"]


def test_check_without_cadence_shows_not_judged_for_silence() -> None:
    data = _full_dict()
    data["operations"]["checks"] = [
        {
            "check_id": "chk_manual",
            "name": "Manual evaluation",
            "category": "model_performance",
            "cadence": None,
            "timezone": "UTC",
            "results": {"metric": "accuracy", "threshold": 0.9, "fires_when": "below"},
        }
    ]
    parts = sections(HandoffPayload.model_validate(data))
    assert (
        "chk_manual Manual evaluation (model_performance): not judged for silence; time zone UTC"
        in parts["operations"]
    )
