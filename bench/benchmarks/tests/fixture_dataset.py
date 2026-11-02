"""Tiny fixture dataset for benchmark replay tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
from demo_world.answer_key import Record
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    Contact,
    DataFeedRef,
    DataRole,
    Feed,
    FileResults,
    Grants,
    HandoffPayload,
    JobFeedRef,
    ModelSpec,
    OnCall,
    OnCallHours,
    Operations,
    Profile,
    Scheduler,
)

from benchmarks.checks import accuracy, null_share

FIXTURE_DAYS = (
    date(2026, 1, 5),
    date(2026, 1, 6),
    date(2026, 1, 7),
    date(2026, 1, 8),
)


class FixtureDataset:
    """A tiny dataset with 4 days and one data_quality null spike on day 2."""

    name = "fixture"

    def days(self) -> Sequence[date]:
        return FIXTURE_DAYS

    def prepare(self, cache: Path) -> None:
        pass

    def key(self) -> Sequence[Record]:
        return [
            Record(
                kind="null_spike",
                category="data_quality",
                column="feature_a",
                start="2026-01-06",
                end="2026-01-06",
            )
        ]

    def handoff(self) -> HandoffPayload:
        model = ModelSpec(
            name="fixture-model",
            profile=Profile(
                objective="Predict churn",
                problem_type="binary_classification",
                target_column="label",
                positive_class="1",
                primary_metric="accuracy",
                metric_rationale="Balanced dataset",
                operating_threshold=0.5,
                label_delay_days=1,
                label_maturity_days=2,
                serving_mode="batch",
                error_costs="Equal",
                cost_ratio=1.0,
                groups=[],
            ),
            predictions=None,
            labels=None,
            commands=[],
            promotion="person",
            environments={},
            production_signal=None,
        )

        operations = Operations(
            scheduler=Scheduler(name="cron", config_location="deploy/crontab"),
            feeds=[
                Feed(
                    name="daily_feed",
                    kind="data",
                    ref=DataFeedRef(connection="data", table="daily_data"),
                    cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                    grace_minutes=120,
                    timezone="UTC",
                ),
                Feed(
                    name="team_check_feed",
                    kind="job",
                    ref=JobFeedRef(
                        ci_workflow=None,
                        file=FileResults(connection="data", path_pattern="results/team*.jsonl"),
                    ),
                    cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                    grace_minutes=120,
                    timezone="UTC",
                ),
            ],
            on_call=[
                OnCall(
                    name="Alice",
                    contacts=[Contact(channel="discord", address="u_alice")],
                    hours=OnCallHours(
                        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        start="00:00",
                        end="23:59",
                    ),
                    timezone="UTC",
                    escalation_order=1,
                )
            ],
            checks=[
                CheckSpec(
                    check_id="chk_null_share",
                    name="Null share check",
                    category="data_quality",
                    cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                    timezone="UTC",
                    results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
                ),
                CheckSpec(
                    check_id="chk_accuracy",
                    name="Accuracy check",
                    category="model_performance",
                    cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                    timezone="UTC",
                    results=FileResults(connection="data", path_pattern="results/team*.jsonl"),
                ),
            ],
            results_location=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
        )

        return HandoffPayload(
            models=[model],
            commands=[],
            data_roles=[
                DataRole(
                    connection="data",
                    table="daily_data",
                    role="training",
                    id_column="id",
                    time_column="date",
                )
            ],
            tracking=[],
            pipeline=[],
            edges=[],
            operations=operations,
            gaps=[],
            grants=Grants(network=False, permission_level="ask"),
        )

    def write_day(self, day: date, data_dir: Path, results_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=UTC)

        # 1. Write daily_data.csv
        # On 2026-01-06 (day 2), introduce null spike in feature_a (50% nulls)
        if day == date(2026, 1, 6):
            features = [1.0, None, 2.0, None, 3.0, None, 4.0, None, 5.0, None]
        else:
            features = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

        df = pd.DataFrame(
            {
                "id": list(range(1, 11)),
                "date": [day.isoformat()] * 10,
                "feature_a": features,
                "label": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
            }
        )
        cast(Any, df).to_csv(data_dir / "daily_data.csv", index=False)

        # 2. Write agent checks into results/checks.jsonl
        rec_null = null_share(
            df,
            "feature_a",
            ran_at=day_start,
            check_id="chk_null_share",
            source_ref="results/checks.jsonl",
        )
        checks_file = results_dir / "checks.jsonl"
        with checks_file.open("a", encoding="utf-8") as f:
            f.write(rec_null.model_dump_json() + "\n")

        # 3. Write team accuracy check into results/team.jsonl
        labels_list = [int(x) for x in cast(Sequence[Any], df["label"])]
        rec_acc = accuracy(
            labels_list,
            [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
            ran_at=day_start,
            check_id="chk_accuracy",
            source_ref="results/team.jsonl",
        )
        team_file = results_dir / "team.jsonl"
        with team_file.open("a", encoding="utf-8") as f:
            f.write(rec_acc.model_dump_json() + "\n")
