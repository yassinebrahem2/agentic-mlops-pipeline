"""Valid I19 payload dicts that the payload tests start from and change one field of."""

from typing import Any

import pytest
from pydantic import ValidationError


def profile() -> dict[str, Any]:
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


def command(**overrides: Any) -> dict[str, Any]:
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


def ci_dispatch(**overrides: Any) -> dict[str, Any]:
    return command(
        **{
            "kind": "rerun",
            "type": "ci_dispatch",
            "template": None,
            "workflow": "rerun.yml",
            "inputs": {"day": "{window_end}"},
            "placeholders": ["window_end"],
            **overrides,
        }
    )


def model(name: str = "cancel", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "profile": profile(),
        "predictions": {"connection": "hotel", "table": "predictions"},
        "labels": {"connection": "hotel", "table": "labels"},
        "commands": [command()],
        "promotion": "team_gate",
        "environments": {"production": "production", "staging": "staging"},
        "production_signal": {"kind": "tracker_alias", "ref": "production"},
    }
    return {**base, **overrides}


def cadence() -> dict[str, Any]:
    return {"cron": "0 6 * * *", "every_minutes": None}


def data_feed(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "bookings",
        "kind": "data",
        "ref": {"connection": "hotel", "table": "bookings"},
        "cadence": cadence(),
        "grace_minutes": 60,
        "timezone": "Europe/Paris",
    }
    return {**base, **overrides}


def job_feed(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "nightly",
        "kind": "job",
        "ref": {"ci_workflow": "nightly.yml", "file": None},
        "cadence": {"cron": None, "every_minutes": 1440},
        "grace_minutes": 30,
        "timezone": "UTC",
    }
    return {**base, **overrides}


def handoff(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "models": [model("cancel"), model("noshow")],
        "commands": [ci_dispatch()],
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
            "feeds": [data_feed(), job_feed()],
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
                    "cadence": cadence(),
                    "timezone": "UTC",
                    "results": {"connection": "hotel", "path_pattern": "dq/*.json"},
                },
                {
                    "check_id": "chk-ci",
                    "name": "Drift",
                    "category": "drift",
                    "cadence": cadence(),
                    "timezone": "UTC",
                    "results": {"workflow": "drift.yml"},
                },
                {
                    "check_id": "chk-mlflow",
                    "name": "Recall",
                    "category": "model_performance",
                    "cadence": cadence(),
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


def connection(name: str = "hotel-data") -> dict[str, Any]:
    return {
        "kind": "local_folder",
        "name": name,
        "settings": {"path": "/data/hotel"},
        "why": "The booking exports",
    }


def connections() -> dict[str, Any]:
    return {"connections": [connection()]}


def error_text(err: pytest.ExceptionInfo[ValidationError]) -> str:
    return " ".join(e["msg"] for e in err.value.errors())
