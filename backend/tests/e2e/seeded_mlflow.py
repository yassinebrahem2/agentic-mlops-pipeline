"""A fake MLflow seeded with the champion of the fix story, and the team's pipeline played on it.

Everything goes through the fake's own functions (`add_experiment`, `add_run`, `finish_run`,
`register_version`, `set_alias`, `put_artifact`). Run it alone with:
python -m tests.e2e.seeded_mlflow --port N
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import uvicorn

from mlops_agents.training.constants import REQUEST_TAG
from tests.e2e.fixture_project import MODEL_NAME, PRODUCTION_ALIAS
from tests.fakes.fake_mlflow import FakeMlflowState, create_fake_mlflow_app

EVALUATION_ARTIFACT: Final = "mlops_agents/evaluation.json"
EVALUATION_COMMIT: Final = "3f9c2d1"
GATE_CONFIG_VERSION: Final = "gate-v1"
CHAMPION_VERSION: Final = "1"
CHALLENGER_VERSION: Final = "2"
CHAMPION_RUN: Final = "run_champion"
CHALLENGER_RUN: Final = "run_challenger"
EXPERIMENT_NAME: Final = "hotel-cancel"
UNREQUESTED_RUN: Final = "req_seeded"


def millis(moment: datetime) -> int:
    return int(moment.astimezone(UTC).timestamp() * 1000)


def metric_set(f1: float) -> dict[str, Any]:
    """A metric set in the shape of the standard evaluation record."""
    return {
        "overall": {"f1": {"value": f1, "ci_low": f1 - 0.03, "ci_high": f1 + 0.03, "n": 480}},
        "per_group": {"country": {"f1": {"value": f1, "ci_low": None, "ci_high": None, "n": 120}}},
    }


def evaluation_record(
    *,
    champion_version: str | None,
    decision: str | None = None,
    reasons: list[str] | None = None,
    challenger_f1: float = 0.71,
    champion_f1: float = 0.52,
) -> bytes:
    """A candidate's `mlops_agents/evaluation.json`, comparable with the champion's: the same
    evaluation commit and gate configuration version."""
    record: dict[str, Any] = {
        "schema_version": 1,
        "evaluation_commit": EVALUATION_COMMIT,
        "gate_config_version": GATE_CONFIG_VERSION,
        "champion_version": champion_version,
        "champion_metrics": metric_set(champion_f1),
        "challenger_metrics": metric_set(challenger_f1),
    }
    if decision is not None:
        record["decision"] = decision
        record["reasons"] = reasons or []
    return json.dumps(record).encode("utf-8")


def seed_champion(state: FakeMlflowState, *, now: datetime) -> str:
    """The champion before the story: a finished run, its version under the production alias,
    and its own evaluation record. Returns the experiment ID."""
    experiment = state.add_experiment(EXPERIMENT_NAME)
    started = now - timedelta(days=40)
    ended = started + timedelta(hours=1)
    state.add_run(
        experiment,
        run_id=CHAMPION_RUN,
        status="FINISHED",
        start_time=millis(started),
        end_time=millis(ended),
    )
    state.register_version(
        MODEL_NAME, CHAMPION_VERSION, run_id=CHAMPION_RUN, creation_timestamp=millis(ended)
    )
    state.set_alias(MODEL_NAME, PRODUCTION_ALIAS, CHAMPION_VERSION)
    state.put_artifact(CHAMPION_RUN, EVALUATION_ARTIFACT, evaluation_record(champion_version=None))
    return experiment


def play_pipeline(
    state: FakeMlflowState,
    *,
    request_id: str,
    now: datetime,
    experiment: str = "0",
    decision: str | None = None,
    reasons: list[str] | None = None,
    move_alias: bool = False,
) -> str:
    """The team's pipeline answering a dispatch: a run tagged with the request ID, a model
    version and its evaluation record (the run ends at `now`, so the record is fresh). In
    `team_gate` the run's end moves the production alias (`move_alias`). Returns the version."""
    started = now - timedelta(minutes=30)
    state.add_run(
        experiment,
        run_id=CHALLENGER_RUN,
        status="FINISHED",
        start_time=millis(started),
        end_time=millis(now),
        tags={REQUEST_TAG: request_id},
    )
    state.register_version(
        MODEL_NAME, CHALLENGER_VERSION, run_id=CHALLENGER_RUN, creation_timestamp=millis(now)
    )
    state.put_artifact(
        CHALLENGER_RUN,
        EVALUATION_ARTIFACT,
        evaluation_record(
            champion_version=CHAMPION_VERSION,
            decision=decision,
            reasons=reasons,
        ),
    )
    if move_alias:
        state.set_alias(MODEL_NAME, PRODUCTION_ALIAS, CHALLENGER_VERSION)
    return CHALLENGER_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description="Seeded fake MLflow REST server for the e2e tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    state = FakeMlflowState()
    now = datetime.now(UTC)
    experiment = seed_champion(state, now=now)
    # The challenger a person is asked to promote: a request nobody made, so no fix links to it.
    play_pipeline(state, request_id=UNREQUESTED_RUN, now=now, experiment=experiment)
    uvicorn.run(
        create_fake_mlflow_app(state), host="127.0.0.1", port=args.port, log_level="warning"
    )


if __name__ == "__main__":
    main()
