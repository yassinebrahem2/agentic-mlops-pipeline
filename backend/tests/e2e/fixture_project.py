"""Fixture project definitions, handoff payloads, and helpers for stage 4-6 E2E tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.rules.contract import RuleLimits, RuleSpec, RuleTerm
from mlops_agents.proposals.payloads import CommandSpec

CONNECTION_NAME: Final = "hotel-data"
MODEL_NAME: Final = "hotel-cancel"
TABLE_BOOKINGS: Final = "bookings"
TABLE_WEATHER: Final = "weather"
TABLE_PREDICTIONS: Final = "predictions"
TABLE_LABELS: Final = "labels"
TABLES: Final = (TABLE_BOOKINGS, TABLE_WEATHER, TABLE_PREDICTIONS, TABLE_LABELS)

SCORING_WORKFLOW: Final = "scoring.yml"
ROLLBACK_HOOK_ID: Final = "rollback"
ROLLBACK_RULE_ID: Final = "rule-rollback"
WEATHER_FEED_NAME: Final = "weather"
TEAM_CHECK_ID: Final = "team-bookings-check"
AGENT_CHECK_ID: Final = "agent-bookings-check"

TIMEZONE: Final = "UTC"
SECOND_DEADLINE_MINUTES: Final[int] = 600  # 10 hours: second deadline falls after morning (09:00)


def monitoring_settings_payload(
    *,
    second_deadline_minutes: int = SECOND_DEADLINE_MINUTES,
    collection_interval_s: int = 60,
    check_grace_minutes: int = 30,
    **kwargs: Any,
) -> dict[str, Any]:
    """Builds a monitoring settings payload for the fixture project.

    Sets second_deadline_minutes so that weather feed's second deadline falls
    after morning on-call hours begin (so it stays high severity).
    """
    payload: dict[str, Any] = {
        "second_deadline_minutes": second_deadline_minutes,
        "collection_interval_s": collection_interval_s,
        "check_grace_minutes": check_grace_minutes,
    }
    payload.update(kwargs)
    return payload


build_fixture_monitoring_settings = monitoring_settings_payload


def rollback_command_spec() -> CommandSpec:
    """The rollback command hook whose entry file exists in the fixture repo."""
    return CommandSpec(
        kind="rollback",
        type="command",
        template="python rollback.py",
        workflow=None,
        inputs={},
        placeholders=[],
        deploys=False,
        entry_files=["rollback.py"],
    )


def rollback_rule_spec(
    hook_id: str = ROLLBACK_HOOK_ID,
    workflow: str = SCORING_WORKFLOW,
) -> RuleSpec:
    """Rule allowing rollback when scoring workflow's latest run failed."""
    return RuleSpec(
        name="rollback-on-scoring-failure",
        why="Roll back immediately when scoring CI workflow fails",
        model=MODEL_NAME,
        terms=[
            RuleTerm(
                check_id=f"ci:{workflow}",
                model=None,
                when=Compare(op="eq", field="fired", value=True),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        hook_id=hook_id,
        hook_kind="rollback",
        limits=RuleLimits(per_incident=1, per_day=5),
    )


def connections_payload(path: str) -> dict[str, Any]:
    return {
        "connections": [
            {
                "kind": LOCAL_FOLDER_KIND,
                "name": CONNECTION_NAME,
                "settings": {"path": path},
                "why": "The hotel bookings, weather, predictions and labels exports",
            }
        ]
    }


def handoff_payload(
    mlflow_url: str = "http://127.0.0.1:5000",
    *,
    scoring_workflow: str = SCORING_WORKFLOW,
    repo_name: str = "octo/repo",
) -> dict[str, Any]:
    """The handoff payload for the simulated monitoring night:
    - Pipeline stages: ingest_bookings (outputs bookings), ingest_weather (outputs weather),
      score (inputs both, outputs predictions, and lists hotel-cancel).
    - Edges into score: predictions is downstream of bookings; weather is not.
    - weather daily feed with deadline in the night.
    - Production signal: ci_job scoring workflow from fake GitHub.
    - Two on-call people with Discord, hours 09:00 to 18:00, escalation order 1 and 2.
    - Rollback command hook.
    """
    file_results = {"connection": CONNECTION_NAME, "path_pattern": "results/*.jsonl"}
    return {
        "models": [
            {
                "name": MODEL_NAME,
                "profile": {
                    "objective": "Predict which bookings will be cancelled",
                    "problem_type": "binary_classification",
                    "target_column": "canceled",
                    "positive_class": "1",
                    "primary_metric": "f1",
                    "metric_rationale": "Balanced precision and recall",
                    "operating_threshold": 0.5,
                    "label_delay_days": 7,
                    "label_maturity_days": 14,
                    "serving_mode": "batch",
                    "error_costs": "A missed cancellation costs three times a false alarm",
                    "cost_ratio": 3.0,
                    "groups": [{"column": "country", "values": None}],
                },
                "predictions": {"connection": CONNECTION_NAME, "table": TABLE_PREDICTIONS},
                "labels": {"connection": CONNECTION_NAME, "table": TABLE_LABELS},
                "commands": [rollback_command_spec().model_dump()],
                "promotion": "person",
                "environments": {"production": "production"},
                "production_signal": {"kind": "ci_job", "ref": scoring_workflow},
            }
        ],
        "commands": [],
        "data_roles": [
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_BOOKINGS,
                "role": "training",
                "id_column": "booking_id",
                "time_column": "arrival_date",
            },
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_WEATHER,
                "role": "other",
                "id_column": "date",
                "time_column": "date",
            },
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_PREDICTIONS,
                "role": "predictions",
                "id_column": "booking_id",
                "time_column": "scored_at",
            },
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_LABELS,
                "role": "labels",
                "id_column": "booking_id",
                "time_column": None,
            },
        ],
        "tracking": [
            {"type": "github_actions", "settings": {"repository": repo_name}, "auth": "none"},
            {"type": "mlflow", "settings": {"url": mlflow_url}, "auth": "none"},
        ],
        "pipeline": [
            {
                "name": "ingest_bookings",
                "entry": "ingest_bookings.py",
                "inputs": [],
                "outputs": [TABLE_BOOKINGS],
            },
            {
                "name": "ingest_weather",
                "entry": "ingest_weather.py",
                "inputs": [],
                "outputs": [TABLE_WEATHER],
            },
            {
                "name": "score",
                "entry": "score.py",
                "inputs": [TABLE_BOOKINGS, TABLE_WEATHER],
                "outputs": [TABLE_PREDICTIONS, MODEL_NAME],
            },
        ],
        "edges": [
            {"from_stage": "ingest_bookings", "to_stage": "score"},
        ],
        "operations": {
            "scheduler": {"name": "cron", "config_location": "deploy/crontab"},
            "feeds": [
                {
                    "name": WEATHER_FEED_NAME,
                    "kind": "data",
                    "ref": {"connection": CONNECTION_NAME, "table": TABLE_WEATHER},
                    "cadence": {"cron": "0 2 * * *", "every_minutes": None},
                    "grace_minutes": 30,
                    "timezone": TIMEZONE,
                }
            ],
            "on_call": [
                {
                    "name": "Alice",
                    "contacts": [{"channel": "discord", "address": "u_alice"}],
                    "hours": {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "09:00",
                        "end": "18:00",
                    },
                    "timezone": TIMEZONE,
                    "escalation_order": 1,
                },
                {
                    "name": "Bob",
                    "contacts": [{"channel": "discord", "address": "u_bob"}],
                    "hours": {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "09:00",
                        "end": "18:00",
                    },
                    "timezone": TIMEZONE,
                    "escalation_order": 2,
                },
            ],
            "checks": [
                {
                    "check_id": TEAM_CHECK_ID,
                    "name": "Team bookings check",
                    "category": "data_quality",
                    "cadence": {"cron": "0 1 * * *", "every_minutes": None},
                    "timezone": TIMEZONE,
                    "results": {"connection": CONNECTION_NAME, "path_pattern": "checks/*.jsonl"},
                },
                {
                    "check_id": AGENT_CHECK_ID,
                    "name": "Agent bookings check",
                    "category": "data_quality",
                    "cadence": {"cron": "30 1 * * *", "every_minutes": None},
                    "timezone": TIMEZONE,
                    "results": {"connection": CONNECTION_NAME, "path_pattern": "checks/*.jsonl"},
                },
            ],
            "results_location": file_results,
        },
        "gaps": [],
        "grants": {"network": False, "permission_level": "ask"},
    }


def playwright_handoff_payload(
    mlflow_url: str = "http://127.0.0.1:5000",
) -> dict[str, Any]:
    """The Playwright variant of the fixture handoff: only file and MLflow sources,
    no feed or check whose deadline can fall inside the run (so no source_failure
    or silence incident reaches the Inbox)."""
    file_results = {"connection": CONNECTION_NAME, "path_pattern": "results/*.jsonl"}
    return {
        "models": [
            {
                "name": MODEL_NAME,
                "profile": {
                    "objective": "Predict which bookings will be cancelled",
                    "problem_type": "binary_classification",
                    "target_column": "canceled",
                    "positive_class": "1",
                    "primary_metric": "f1",
                    "metric_rationale": "Balanced precision and recall",
                    "operating_threshold": 0.5,
                    "label_delay_days": 7,
                    "label_maturity_days": 14,
                    "serving_mode": "batch",
                    "error_costs": "A missed cancellation costs three times a false alarm",
                    "cost_ratio": 3.0,
                    "groups": [{"column": "country", "values": None}],
                },
                "predictions": {"connection": CONNECTION_NAME, "table": TABLE_PREDICTIONS},
                "labels": {"connection": CONNECTION_NAME, "table": TABLE_LABELS},
                "commands": [rollback_command_spec().model_dump()],
                "promotion": "person",
                "environments": {"production": "production"},
                "production_signal": {"kind": "file", "ref": f"results/{TABLE_PREDICTIONS}.jsonl"},
            }
        ],
        "commands": [],
        "data_roles": [
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_BOOKINGS,
                "role": "training",
                "id_column": "booking_id",
                "time_column": "arrival_date",
            },
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_PREDICTIONS,
                "role": "predictions",
                "id_column": "booking_id",
                "time_column": "scored_at",
            },
            {
                "connection": CONNECTION_NAME,
                "table": TABLE_LABELS,
                "role": "labels",
                "id_column": "booking_id",
                "time_column": None,
            },
        ],
        "tracking": [
            {"type": "mlflow", "settings": {"url": mlflow_url}, "auth": "none"},
        ],
        "pipeline": [
            {
                "name": "ingest_bookings",
                "entry": "ingest_bookings.py",
                "inputs": [],
                "outputs": [TABLE_BOOKINGS],
            },
            {
                "name": "score",
                "entry": "score.py",
                "inputs": [TABLE_BOOKINGS],
                "outputs": [TABLE_PREDICTIONS, MODEL_NAME],
            },
        ],
        "edges": [
            {"from_stage": "ingest_bookings", "to_stage": "score"},
        ],
        "operations": {
            "scheduler": {"name": "cron", "config_location": "deploy/crontab"},
            "feeds": [],
            "on_call": [
                {
                    "name": "Alice",
                    "contacts": [{"channel": "discord", "address": "u_alice"}],
                    "hours": {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "09:00",
                        "end": "18:00",
                    },
                    "timezone": TIMEZONE,
                    "escalation_order": 1,
                },
            ],
            "checks": [],
            "results_location": file_results,
        },
        "gaps": [],
        "grants": {"network": False, "permission_level": "ask"},
    }


def write_results_record(
    folder: Path,
    record: dict[str, Any],
    filename: str = "results.jsonl",
) -> Path:
    """Writes or appends a JSON Lines record to a file in the given connection folder."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    line = json.dumps(record) + "\n"
    with target.open("a", encoding="utf-8") as f:
        f.write(line)
    return target


def create_fixture_repo(path: Path, git_env: dict[str, str]) -> Path:
    """Creates a git repository with the rollback hook entry file and pipelines committed."""
    from tests.conftest import make_repo

    files = {
        "README.md": "# Hotel Cancel Pipeline\n",
        "rollback.py": "print('Rollback hook executed successfully')\n",
        "ingest_bookings.py": "print('Bookings ingested')\n",
        "ingest_weather.py": "print('Weather ingested')\n",
        "score.py": "print('Scored')\n",
    }
    return make_repo(path, files, git_env)


def write_fixture_data_folder(folder: Path) -> None:
    """Writes the CSV tables for bookings, weather, predictions and labels."""
    folder.mkdir(parents=True, exist_ok=True)
    bookings = [
        "booking_id,arrival_date,country,lead_time,canceled",
        "B001,2026-10-31,PRT,10,0",
        "B002,2026-10-31,GBR,20,1",
        "B003,2026-10-31,FRA,30,0",
    ]
    weather = [
        "date,temp,rain",
        "2026-10-30,18.5,0.0",
        "2026-10-31,16.0,2.5",
    ]
    predictions = [
        "booking_id,scored_at,score",
        "B001,2026-10-31T06:00:00,0.1",
        "B002,2026-10-31T06:00:00,0.8",
        "B003,2026-10-31T06:00:00,0.2",
    ]
    labels = [
        "booking_id,canceled",
        "B001,0",
        "B002,1",
        "B003,0",
    ]
    for table, lines in (
        (TABLE_BOOKINGS, bookings),
        (TABLE_WEATHER, weather),
        (TABLE_PREDICTIONS, predictions),
        (TABLE_LABELS, labels),
    ):
        (folder / f"{table}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
