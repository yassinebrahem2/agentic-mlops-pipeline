"""Batch pipeline telemetry dataset, scenario, and injections."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import numpy as np
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
from demo_world.answer_key import Category, Record
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    Contact,
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

DEFAULT_SCENARIO_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent.parent / "scenarios" / "telemetry.toml"
)
DEFAULT_SEED: Final[int] = 42
DEFAULT_ROWS_PER_DAY: Final[int] = 50


def telemetry_business_days(start_date: date = date(2026, 1, 5), count: int = 60) -> list[date]:
    """Generates `count` business days starting from `start_date`."""
    days: list[date] = []
    current = start_date
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


@dataclass(frozen=True)
class MissingFeedInjection:
    """A feed stops arriving for some business days (no file written: silence)."""

    start: date
    end: date
    kind: str = "missing_feed"
    category: Category = "operational"


@dataclass(frozen=True)
class LateArrivalInjection:
    """Files arrive after the feed's deadline plus grace (written on a later day)."""

    start: date
    end: date
    delay_days: int = 2
    kind: str = "late_arrival"
    category: Category = "operational"


@dataclass(frozen=True)
class ParserFailureInjection:
    """A results file the parser cannot read (parser failure)."""

    start: date
    end: date
    kind: str = "parser_failure"
    category: Category = "operational"


type TelemetryInjection = MissingFeedInjection | LateArrivalInjection | ParserFailureInjection


def load_telemetry_scenario(path: Path) -> list[TelemetryInjection]:
    """Loads injection definitions from a scenario TOML file."""
    if not path.is_file():
        raise FileNotFoundError(f"Telemetry scenario file not found: {path}")

    with path.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)

    injections: list[TelemetryInjection] = []
    raw_injections: object = data.get("injection")
    if not isinstance(raw_injections, list) or not raw_injections:
        raise ValueError(f"Scenario file {path} has no injections defined under [[injection]]")

    for raw_item in cast(list[object], raw_injections):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Invalid injection item in {path}: {raw_item}")
        item: dict[str, Any] = cast(dict[str, Any], raw_item)
        kind: object = item.get("kind")
        if not kind or not isinstance(kind, str):
            raise ValueError(f"Injection missing required 'kind' in {path}")
        start_raw: object = item.get("start")
        end_raw: object = item.get("end")
        if not start_raw or not end_raw:
            raise ValueError(f"Injection {kind} missing required 'start' or 'end' date in {path}")
        start_d = date.fromisoformat(str(start_raw))
        end_d = date.fromisoformat(str(end_raw))

        if kind == "missing_feed":
            injections.append(MissingFeedInjection(start=start_d, end=end_d))
        elif kind == "late_arrival":
            delay = item.get("delay_days", 2)
            injections.append(LateArrivalInjection(start=start_d, end=end_d, delay_days=int(delay)))
        elif kind == "parser_failure":
            injections.append(ParserFailureInjection(start=start_d, end=end_d))
        else:
            raise ValueError(f"Unknown telemetry injection kind: {kind}")

    return injections


def _generate_events_frame(
    day: date, rows: int = DEFAULT_ROWS_PER_DAY, seed: int | None = None
) -> pd.DataFrame:
    """Generates a deterministic synthetic daily batch events DataFrame."""
    rng = np.random.default_rng(seed if seed is not None else 42)
    ids = list(range(1, rows + 1))
    durations = [float(d) for d in np.round(rng.uniform(10.0, 300.0, size=rows), 2)]
    records_proc = [int(r) for r in rng.integers(100, 10000, size=rows)]
    day_str = day.isoformat()
    timestamps = [f"{day_str}T00:{min(i, 59):02d}:00Z" for i in range(rows)]

    data: dict[str, Any] = {
        "id": ids,
        "timestamp": timestamps,
        "job_name": ["batch_pipeline_job"] * rows,
        "duration_seconds": durations,
        "records_processed": records_proc,
        "status": ["SUCCESS"] * rows,
    }
    return pd.DataFrame(data)


class TelemetryDataset:
    """60 business days of batch pipeline telemetry and operational injections."""

    name: str = "telemetry"
    scenario: str = "telemetry"
    seed: int | None = None

    def __init__(
        self,
        *,
        seed: int | None = DEFAULT_SEED,
        scenario_path: Path | None = None,
        days_override: Sequence[date] | None = None,
        injections_override: Sequence[TelemetryInjection] | None = None,
    ) -> None:
        self.seed = seed
        self._scenario_path = scenario_path or DEFAULT_SCENARIO_PATH

        if days_override is not None:
            self._days = list(days_override)
        else:
            self._days = telemetry_business_days(start_date=date(2026, 1, 5), count=60)

        if injections_override is not None:
            self._injections = list(injections_override)
        elif self._scenario_path.is_file():
            self._injections = load_telemetry_scenario(self._scenario_path)
        else:
            self._injections = []

    def days(self) -> Sequence[date]:
        """Sequence of business dates for this dataset, in chronological order."""
        return self._days

    def prepare(self, cache: Path) -> None:
        """Synthetic dataset: ensures cache directory exists idempotently."""
        cache.mkdir(parents=True, exist_ok=True)

    def key(self) -> Sequence[Record]:
        """Returns the ground truth answer key records for all injected problems."""
        records: list[Record] = []
        for inj in self._injections:
            records.append(
                Record(
                    kind=inj.kind,
                    category=inj.category,
                    column=None,
                    start=inj.start.isoformat(),
                    end=inj.end.isoformat(),
                )
            )
        return records

    def handoff(self) -> HandoffPayload:
        """Declares the batch pipeline handoff: feeds, checks, and operations."""
        model = ModelSpec(
            name="pipeline-telemetry-model",
            profile=Profile(
                objective="Telemetry monitoring for batch pipeline",
                problem_type="binary_classification",
                target_column="status",
                positive_class="1",
                primary_metric="accuracy",
                metric_rationale="Operational batch pipeline health",
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
                    name="chk_pipeline_job",
                    kind="job",
                    ref=JobFeedRef(
                        ci_workflow=None,
                        file=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
                    ),
                    cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                    grace_minutes=120,
                    timezone="UTC",
                ),
            ],
            on_call=[
                OnCall(
                    name="Telemetry Ops",
                    contacts=[Contact(channel="discord", address="telemetry_ops")],
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
                    check_id="chk_pipeline_job",
                    name="Pipeline job check",
                    category="operational",
                    cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                    timezone="UTC",
                    results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
                )
            ],
            results_location=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
        )

        return HandoffPayload(
            models=[model],
            commands=[],
            data_roles=[
                DataRole(
                    connection="data",
                    table="pipeline_events",
                    role="training",
                    id_column="id",
                    time_column="timestamp",
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
        """Writes that day's feed files and job results, honoring operational injections."""
        events_dir = data_dir / "pipeline_events"
        events_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=UTC)

        # 1. Daily batch events data file (always written for pipeline telemetry)
        events_file = events_dir / f"events_{day.isoformat()}.csv"
        seed_val = (self.seed + day.toordinal()) if self.seed is not None else None
        df = _generate_events_frame(day, seed=seed_val)
        cast(Any, df).to_csv(events_file, index=False)

        # 2. Results location check records / operational injections
        is_missing_today = any(
            isinstance(inj, MissingFeedInjection) and inj.start <= day <= inj.end
            for inj in self._injections
        )
        is_late_today = any(
            isinstance(inj, LateArrivalInjection) and inj.start <= day <= inj.end
            for inj in self._injections
        )
        is_parser_failure_today = any(
            isinstance(inj, ParserFailureInjection) and inj.start <= day <= inj.end
            for inj in self._injections
        )

        if is_parser_failure_today:
            # Write a malformed unparseable file for this day
            err_file = results_dir / f"checks_err_{day.isoformat()}.jsonl"
            with err_file.open("w", encoding="utf-8") as f:
                f.write("bad unparseable json line\n")
            return

        dates_to_write_results: list[date] = []
        if not is_missing_today and not is_late_today:
            dates_to_write_results.append(day)

        # Check if any earlier delayed files arrive on `day`
        for inj in self._injections:
            if isinstance(inj, LateArrivalInjection):
                for candidate_day in self._days:
                    if inj.start <= candidate_day <= inj.end:
                        c_idx = self._days.index(candidate_day)
                        target_idx = c_idx + inj.delay_days
                        if target_idx < len(self._days) and self._days[target_idx] == day:
                            dates_to_write_results.append(candidate_day)

        for target_date in dates_to_write_results:
            results_file = results_dir / f"checks_{target_date.isoformat()}.jsonl"
            # Late arrivals arrive stamped with the current day's time
            run_dt = (
                day_start
                if target_date != day
                else datetime(
                    target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=UTC
                )
            )
            rec = {
                "check_id": "chk_pipeline_job",
                "ran_at": run_dt.isoformat(),
                "fired": False,
                "value": 0.0,
                "category": "operational",
                "subject": "chk_pipeline_job",
                "detail": {"status": "ok"},
            }
            with results_file.open("w", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
