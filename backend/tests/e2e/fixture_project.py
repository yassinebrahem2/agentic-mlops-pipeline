"""Fixture project definitions, handoff payloads, and helpers for stage 4-6 E2E tests."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.contract import (
    ParserMapping,
    ParserPayload,
    ParserSpec,
    ResultRecord,
    source_id,
)
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

TEAM_FEED_NAME: Final = "team-check-job"
TEAM_RESULTS_PATTERN: Final = "team/*.jsonl"
TEAM_SOURCE_ID: Final = source_id("file", TEAM_RESULTS_PATTERN, CONNECTION_NAME)

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
                "inputs": [TABLE_BOOKINGS],
                "outputs": [TABLE_PREDICTIONS, MODEL_NAME],
            },
        ],
        "edges": [
            {"from_stage": "ingest_bookings", "to_stage": "score"},
            {"from_stage": "ingest_weather", "to_stage": "score"},
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
                },
                {
                    # The team's own check job: its output is in the team's format, so a
                    # mapping parser reads it. Its deadline falls after the night.
                    "name": TEAM_FEED_NAME,
                    "kind": "job",
                    "ref": {
                        "ci_workflow": None,
                        "file": {
                            "connection": CONNECTION_NAME,
                            "path_pattern": TEAM_RESULTS_PATTERN,
                        },
                    },
                    "cadence": {"cron": "0 12 * * *", "every_minutes": None},
                    "grace_minutes": 30,
                    "timezone": TIMEZONE,
                },
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
                    "results": {
                        "connection": CONNECTION_NAME,
                        "path_pattern": TEAM_RESULTS_PATTERN,
                    },
                },
                {
                    "check_id": AGENT_CHECK_ID,
                    "name": "Agent bookings check",
                    "category": "data_quality",
                    "cadence": {"cron": "30 1 * * *", "every_minutes": None},
                    "timezone": TIMEZONE,
                    "results": {"connection": CONNECTION_NAME, "path_pattern": "checks/*.jsonl"},
                },
                {
                    "check_id": f"ci:{SCORING_WORKFLOW}",
                    "name": "Scoring CI check",
                    "category": "operational",
                    "cadence": {"cron": "0 3 * * *", "every_minutes": None},
                    "timezone": TIMEZONE,
                    "results": {"workflow": SCORING_WORKFLOW},
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


def team_check_line(*, ran_at: datetime, table: str, failed_rows: int) -> dict[str, Any]:
    """One line of the team's own check output, in the team's format (not ours)."""
    return {
        "ts": ran_at.isoformat(),
        "table": table,
        "failed_rows": failed_rows,
        "info": {"issue": f"team {table} anomaly"},
    }


def write_team_check_record(
    folder: Path, *, ran_at: datetime, table: str, failed_rows: int = 3
) -> Path:
    """Appends one team-format line to the team's JSON Lines file in the connection folder."""
    return write_results_record(
        folder,
        team_check_line(ran_at=ran_at, table=table, failed_rows=failed_rows),
        "bookings-check.jsonl",
    )


def team_parser_payload(*, ran_at: datetime) -> dict[str, Any]:
    """The proposal payload of the team check's mapping parser; its sample is a real line of
    the team's format with the record the mapping gives."""
    mapping = ParserMapping(
        format="jsonl",
        fields={
            "ran_at": "ts",
            "subject": "table",
            "value": "failed_rows",
            "detail": "info",
        },
        # The mapping condition reads the mapped fields
        fired_when=Compare(op="gt", field="value", value=0),
        defaults={"check_id": TEAM_CHECK_ID, "category": "data_quality"},
    )
    sample = team_check_line(ran_at=ran_at, table=TABLE_BOOKINGS, failed_rows=3)
    record = ResultRecord(
        check_id=TEAM_CHECK_ID,
        origin="team",
        ran_at=ran_at,
        fired=True,
        category="data_quality",
        subject=TABLE_BOOKINGS,
        value=3.0,
        detail=sample["info"],
        source_ref="team/bookings-check.jsonl",
    )
    payload = ParserPayload(
        spec=ParserSpec(source_id=TEAM_SOURCE_ID, mapping=mapping),
        sample_input=json.dumps(sample),
        sample_records=[record],
    )
    return payload.model_dump(mode="json")


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


# ---------------------------------------------------------------------------------------------
# The stage 5 story: a confirmed decay, the fix, the promotion (test_fix_and_promotion.py).
# ---------------------------------------------------------------------------------------------

PromotionMode = Literal["person", "team_gate"]

# The workflow sits at the repository root so that the fake GitHub's one-segment route takes it.
TRAIN_WORKFLOW: Final = "train.yml"
TRAIN_ENTRY: Final = "train.py"
DEPLOY_ENTRY: Final = "deploy.py"
GITHUB_REPOSITORY: Final = "octo/repo"
GITHUB_ORIGIN: Final = f"https://github.com/{GITHUB_REPOSITORY}.git"
PRODUCTION_ALIAS: Final = "production"
LABEL_CHECK_ID: Final = "mp-label-check"
LABEL_MATURITY_DAYS: Final = 14
DEPLOY_COMMAND: Final = "python deploy.py {candidate_version}"


def train_command_spec(*, deploys: bool) -> CommandSpec:
    """The `train` hook: a dispatch of the team's workflow, which writes the request ID it
    receives as the run's `REQUEST_TAG`."""
    return CommandSpec(
        kind="train",
        type="ci_dispatch",
        template=None,
        workflow=TRAIN_WORKFLOW,
        inputs={
            "request_id": "{request_id}",
            "window_start": "{window_start}",
            "window_end": "{window_end}",
        },
        placeholders=["request_id", "window_start", "window_end"],
        deploys=deploys,
        entry_files=[TRAIN_ENTRY],
    )


def deploy_command_spec() -> CommandSpec:
    """The `deploy` hook of a `person` model: a command that runs in the sandbox."""
    return CommandSpec(
        kind="deploy",
        type="command",
        template=DEPLOY_COMMAND,
        workflow=None,
        inputs={},
        placeholders=["candidate_version"],
        deploys=True,
        entry_files=[DEPLOY_ENTRY],
    )


def fix_story_handoff_payload(
    mlflow_url: str,
    promotion: PromotionMode,
) -> dict[str, Any]:
    """The handoff of the fix story: one model with a tracker-alias production signal and its
    train hook (and, for `person`, its deploy hook), an MLflow tracking source, one on-call
    person, and no feed or check whose deadline could open an incident of its own."""
    payload = playwright_handoff_payload(mlflow_url)
    model = payload["models"][0]
    commands = [train_command_spec(deploys=promotion == "team_gate").model_dump()]
    if promotion == "person":
        commands.append(deploy_command_spec().model_dump())
    model["commands"] = commands
    model["promotion"] = promotion
    model["production_signal"] = {"kind": "tracker_alias", "ref": PRODUCTION_ALIAS}
    model["environments"] = {PRODUCTION_ALIAS: "production"}
    model["profile"]["label_maturity_days"] = LABEL_MATURITY_DAYS
    return payload


def create_fix_repo(path: Path, git_env: dict[str, str]) -> Path:
    """The team's repository at the approved commit: the train hook's workflow file and every
    declared entry file (the pin check reads them), with a GitHub origin."""
    from tests.agents.tasks.helpers import cmd
    from tests.conftest import make_repo

    files = {
        "README.md": "# Hotel Cancel Pipeline\n",
        TRAIN_WORKFLOW: "name: train\non: workflow_dispatch\n",
        TRAIN_ENTRY: "print('training')\n",
        DEPLOY_ENTRY: "print('deploying')\n",
        "evaluate.py": "print('evaluating')\n",
        "ingest_bookings.py": "print('Bookings ingested')\n",
        "score.py": "print('Scored')\n",
    }
    repo = make_repo(path, files, git_env)
    cmd(["git", "remote", "add", "origin", GITHUB_ORIGIN], cwd=repo, env=git_env)
    return repo


def make_bare_origin(path: Path, git_env: dict[str, str], repo: Path) -> Path:
    """A bare repository holding the project's branch, which `git_instead_of` serves for the
    GitHub origin so that a pushed pull request branch has somewhere to go."""
    from tests.agents.tasks.helpers import cmd

    cmd(["git", "clone", "--bare", str(repo), str(path)], env=git_env)
    return path


@contextmanager
def git_instead_of(config: Path, bare: Path) -> Generator[None]:
    """Serves the GitHub origin from `bare` for the git commands of this process: a task's
    fetch and a pull request branch's push. `git remote get-url` then answers the bare path, so
    a test that dispatches a workflow (which reads the GitHub URL from it) does not use this."""
    from tests.agents.tasks.helpers import cmd

    cmd(["git", "config", "--file", str(config), f"url.{bare.as_uri()}.insteadOf", GITHUB_ORIGIN])
    previous = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(config)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = previous


def label_check_record(*, ran_at: datetime) -> ResultRecord:
    """The Model Performance agent's label-based check firing on the model: the labels
    confirm the decay."""
    return ResultRecord(
        check_id=LABEL_CHECK_ID,
        origin="agent",
        ran_at=ran_at,
        fired=True,
        category="model_performance",
        subject=TABLE_PREDICTIONS,
        model=MODEL_NAME,
        value=0.52,
        detail={"f1": 0.52, "labels": 480},
        source_ref="checks/mp-labels.jsonl",
    )
