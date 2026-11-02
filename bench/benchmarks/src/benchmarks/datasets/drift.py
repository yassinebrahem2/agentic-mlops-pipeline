"""Synthetic drift dataset, injections, scenario, and model."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import lightgbm as lgb  # pyright: ignore[reportMissingTypeStubs]
import numpy as np
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
from demo_world.answer_key import Category, Record
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
from river.datasets import synth

from benchmarks.checks import (
    accuracy,
    null_share,
    out_of_range,
    pr_auc_matured,
    psi_drift,
)

FEATURE_COLUMNS: Final[list[str]] = [
    "salary",
    "commission",
    "age",
    "elevel",
    "car",
    "zipcode",
    "hvalue",
    "hyears",
    "loan",
]
TARGET_COLUMN: Final[str] = "y"

DEFAULT_SCENARIO_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent.parent / "scenarios" / "drift.toml"
)
DEFAULT_SEED: Final[int] = 42
DEFAULT_SAMPLES_PER_DAY: Final[int] = 500


def drift_business_days(start_date: date = date(2026, 1, 5), count: int = 90) -> list[date]:
    """Generates `count` business days starting from `start_date`."""
    days: list[date] = []
    current = start_date
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def generate_drift_frame(
    n_samples: int = 10000,
    *,
    seed: int | None = None,
    position: int = 5000,
    width: int = 500,
    base_function: int = 0,
    drift_function: int = 2,
) -> pd.DataFrame:
    """Generates a synthetic dataset using river ConceptDriftStream over Agrawal."""
    synth_module = cast(Any, synth)
    stream = synth_module.ConceptDriftStream(
        stream=synth_module.Agrawal(classification_function=base_function, seed=seed),
        drift_stream=synth_module.Agrawal(classification_function=drift_function, seed=seed),
        position=position,
        width=width,
        seed=seed,
    )
    samples: list[tuple[dict[str, Any], Any]] = list(stream.take(n_samples))
    rows: list[dict[str, Any]] = []
    for x, y in samples:
        row: dict[str, Any] = dict(x)
        row[TARGET_COLUMN] = int(y)
        rows.append(row)
    return pd.DataFrame(rows)


def _eval_agrawal_function(func_id: int, row: Mapping[str, Any]) -> int:
    """Evaluates an Agrawal classification function on row features."""
    salary = float(row["salary"])
    age = float(row["age"])
    elevel = int(row["elevel"])
    car = int(row["car"])
    loan = float(row["loan"])

    if func_id == 0:
        return int((age < 40) or (age >= 60))
    if func_id == 1:
        if age < 40:
            return int(50000 <= salary <= 100000)
        if age < 60:
            return int(75000 <= salary <= 125000)
        return int(25000 <= salary <= 75000)
    if func_id == 2:
        if age < 40:
            return int((elevel == 0) or (elevel == 1))
        if age < 60:
            return int(elevel in (1, 2, 3))
        return int(elevel in (2, 3, 4))
    if func_id == 3:
        if age < 40:
            return int(elevel in (0, 1) and (25000 <= loan <= 75000))
        if age < 60:
            return int(elevel in (1, 2, 3) and (50000 <= loan <= 100000))
        return int(elevel in (2, 3, 4) and (75000 <= loan <= 125000))
    if func_id == 4:
        if age < 40:
            return int((50000 <= salary <= 100000) and (100000 <= loan <= 300000))
        if age < 60:
            return int((75000 <= salary <= 125000) and (200000 <= loan <= 400000))
        return int((25000 <= salary <= 75000) and (300000 <= loan <= 500000))
    if func_id == 5:
        if age < 40:
            return int((50000 <= salary <= 100000) and (1 <= car <= 10))
        if age < 60:
            return int((75000 <= salary <= 125000) and (11 <= car <= 15))
        return int((25000 <= salary <= 75000) and (16 <= car <= 20))
    return int((age < 40) or (age >= 60))


@dataclass(frozen=True, kw_only=True)
class DriftInjection:
    """Base class for scenario injections on synthetic drift data."""

    kind: str
    category: Category
    column: str | None
    start: date
    end: date
    position: int | None = None
    width: int | None = None

    def active(self, day: date) -> bool:
        return self.start <= day <= self.end

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        return df

    def record(self) -> Record:
        return Record(
            kind=self.kind,
            category=self.category,
            column=self.column,
            start=self.start.isoformat(),
            end=self.end.isoformat(),
        )


@dataclass(frozen=True, kw_only=True)
class NullSpikeInjection(DriftInjection):
    """Spikes missing values in a column for active days."""

    share: float

    def __init__(
        self,
        *,
        column: str,
        share: float,
        start: date,
        end: date,
    ) -> None:
        object.__setattr__(self, "kind", "null_spike")
        object.__setattr__(self, "category", "data_quality")
        object.__setattr__(self, "column", column)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "share", share)
        object.__setattr__(self, "position", None)
        object.__setattr__(self, "width", None)

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or self.column is None or len(df) == 0:
            return df
        result = df.copy()
        n = len(result)
        hit_count = max(1, round(self.share * n))
        indices = np.linspace(0, n - 1, hit_count, dtype=int)
        vals = [float(v) for v in cast(Sequence[Any], result[self.column])]
        for idx in indices:
            vals[int(idx)] = float("nan")
        result[self.column] = vals
        return result


@dataclass(frozen=True, kw_only=True)
class OutOfRangeInjection(DriftInjection):
    """Injects out-of-range values into a column for active days."""

    value: float
    share: float

    def __init__(
        self,
        *,
        column: str,
        value: float,
        share: float,
        start: date,
        end: date,
    ) -> None:
        object.__setattr__(self, "kind", "out_of_range")
        object.__setattr__(self, "category", "data_quality")
        object.__setattr__(self, "column", column)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "share", share)
        object.__setattr__(self, "position", None)
        object.__setattr__(self, "width", None)

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or self.column is None or len(df) == 0:
            return df
        result = df.copy()
        n = len(result)
        hit_count = max(1, round(self.share * n))
        indices = np.linspace(0, n - 1, hit_count, dtype=int)
        vals = [float(v) for v in cast(Sequence[Any], result[self.column])]
        for idx in indices:
            vals[int(idx)] = self.value
        result[self.column] = vals
        return result


@dataclass(frozen=True, kw_only=True)
class CovariateShiftInjection(DriftInjection):
    """Shifts feature distribution by adding an offset for active days."""

    shift: float

    def __init__(
        self,
        *,
        column: str,
        shift: float,
        start: date,
        end: date,
        position: int | None = None,
        width: int | None = None,
    ) -> None:
        object.__setattr__(self, "kind", "covariate_shift")
        object.__setattr__(self, "category", "drift")
        object.__setattr__(self, "column", column)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "shift", shift)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "width", width)

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or self.column is None or len(df) == 0:
            return df
        result = df.copy()
        vals = [float(v) + self.shift for v in cast(Sequence[Any], result[self.column])]
        result[self.column] = vals
        return result


@dataclass(frozen=True, kw_only=True)
class ConceptDriftInjection(DriftInjection):
    """Applies concept drift (changes P(y|x)) by altering class generation function."""

    function: int

    def __init__(
        self,
        *,
        function: int,
        start: date,
        end: date,
        position: int | None = None,
        width: int | None = None,
    ) -> None:
        object.__setattr__(self, "kind", "concept_drift")
        object.__setattr__(self, "category", "model_performance")
        object.__setattr__(self, "column", None)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "function", function)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "width", width)

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or len(df) == 0:
            return df
        result = df.copy()
        records = cast(list[dict[str, Any]], cast(Any, result).to_dict(orient="records"))
        new_targets = [_eval_agrawal_function(self.function, r) for r in records]
        result[TARGET_COLUMN] = new_targets
        return result


def load_drift_scenario(path: Path) -> list[DriftInjection]:
    """Loads injection definitions from scenario TOML file."""
    if not path.is_file():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with path.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)

    injections: list[DriftInjection] = []
    raw_injections = data.get("injection")
    if not isinstance(raw_injections, list) or not raw_injections:
        raise ValueError(f"Scenario file {path} has no injections defined under [[injection]]")

    for raw_item in cast(list[object], raw_injections):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Invalid injection item in {path}: {raw_item}")
        item: dict[str, Any] = cast(dict[str, Any], raw_item)
        kind = str(item.get("kind", ""))
        start_d = date.fromisoformat(str(item["start"]))
        end_d = date.fromisoformat(str(item["end"]))
        pos_raw = item.get("position")
        width_raw = item.get("width")
        pos = int(pos_raw) if isinstance(pos_raw, (int, float)) else None
        w = int(width_raw) if isinstance(width_raw, (int, float)) else None

        if kind == "null_spike":
            injections.append(
                NullSpikeInjection(
                    column=str(item["column"]),
                    share=float(item["share"]),
                    start=start_d,
                    end=end_d,
                )
            )
        elif kind == "out_of_range":
            injections.append(
                OutOfRangeInjection(
                    column=str(item["column"]),
                    value=float(item["value"]),
                    share=float(item["share"]),
                    start=start_d,
                    end=end_d,
                )
            )
        elif kind == "covariate_shift":
            injections.append(
                CovariateShiftInjection(
                    column=str(item["column"]),
                    shift=float(item["shift"]),
                    start=start_d,
                    end=end_d,
                    position=pos,
                    width=w,
                )
            )
        elif kind == "concept_drift":
            injections.append(
                ConceptDriftInjection(
                    function=int(item.get("function", 2)),
                    start=start_d,
                    end=end_d,
                    position=pos,
                    width=w,
                )
            )
        else:
            raise ValueError(f"Unsupported injection kind {kind!r} in {path}")

    return injections


def train_drift_model(train_df: pd.DataFrame) -> lgb.LGBMClassifier:
    """Trains LightGBM binary classifier on training features."""
    X = cast(Any, train_df[FEATURE_COLUMNS])
    y = [int(v) for v in cast(Sequence[Any], train_df[TARGET_COLUMN])]
    clf = lgb.LGBMClassifier(
        n_estimators=50,
        min_child_samples=1,
        random_state=42,
        verbose=-1,
    )
    cast(Any, clf).fit(X, y)
    return clf


class DriftDataset:
    """Synthetic concept drift benchmark dataset implementation."""

    name: str = "drift"
    scenario: str = "drift"
    seed: int | None = None

    def __init__(
        self,
        *,
        frame: pd.DataFrame | None = None,
        days: Sequence[date] | None = None,
        scenario_path: Path | None = None,
        injections: Sequence[DriftInjection] | None = None,
        operating_threshold: float = 0.5,
        seed: int | None = None,
        samples_per_day: int = DEFAULT_SAMPLES_PER_DAY,
        train_days: int | None = None,
    ) -> None:
        self.seed = seed
        self._seed = seed if seed is not None else DEFAULT_SEED
        self._frame = frame
        self._scenario_path = scenario_path or DEFAULT_SCENARIO_PATH
        self._operating_threshold = operating_threshold
        self._samples_per_day = samples_per_day

        if injections is not None:
            self._injections = list(injections)
        else:
            self._injections = load_drift_scenario(self._scenario_path)

        if days is not None:
            self._business_days = list(days)
        else:
            self._business_days = drift_business_days(date(2026, 1, 5), count=90)

        if train_days is not None:
            self._train_days = min(train_days, len(self._business_days))
        else:
            self._train_days = min(20, max(1, len(self._business_days) - 1))

        self._cache_dir: Path | None = None
        self._splits: dict[date, pd.DataFrame] | None = None
        self._model: lgb.LGBMClassifier | None = None
        self._reference_df: pd.DataFrame | None = None
        self._prev_scores: list[float] | None = None
        self._prev_y_true: list[int] | None = None

    @property
    def injections(self) -> list[DriftInjection]:
        return list(self._injections)

    def days(self) -> Sequence[date]:
        return self._business_days

    def key(self) -> Sequence[Record]:
        return [inj.record() for inj in self._injections]

    def prepare(self, cache: Path) -> None:
        self._cache_dir = cache
        cache.mkdir(parents=True, exist_ok=True)
        if self._frame is None:
            total_samples = len(self._business_days) * self._samples_per_day
            pq_file = cache / f"drift_{self._seed}_{len(self._business_days)}.pq"
            if not pq_file.exists():
                df = generate_drift_frame(total_samples, seed=self._seed)
                cast(Any, df).to_parquet(pq_file)

    def _ensure_data_initialized(self, cache: Path | None = None) -> None:
        if self._splits is not None:
            return

        total_samples = len(self._business_days) * self._samples_per_day
        if self._frame is not None:
            raw_df = self._frame.copy()
        else:
            cache_dir = cache or self._cache_dir
            pq_file = (
                cache_dir / f"drift_{self._seed}_{len(self._business_days)}.pq"
                if cache_dir
                else None
            )
            if pq_file and pq_file.exists():
                raw_df = cast(pd.DataFrame, cast(Any, pd).read_parquet(pq_file))
            else:
                raw_df = generate_drift_frame(total_samples, seed=self._seed)

        # Split into days
        n_rows = len(raw_df)
        n_days = len(self._business_days)
        splits = np.array_split(np.arange(n_rows), n_days)
        self._splits = {}
        iloc_obj = cast(Any, raw_df.iloc)
        for d, indices in zip(self._business_days, splits, strict=True):
            subset = cast(pd.DataFrame, iloc_obj[indices].copy().reset_index(drop=True))
            self._splits[d] = subset

        # Train model on first train_days
        train_days_list = self._business_days[: self._train_days]
        train_df = pd.concat([self._splits[d] for d in train_days_list], ignore_index=True)
        self._reference_df = train_df
        self._model = train_drift_model(train_df)

    def handoff(self) -> HandoffPayload:
        model = ModelSpec(
            name="synthetic-drift-model",
            profile=Profile(
                objective="Detect concept drift and data issues in synthetic stream",
                problem_type="binary_classification",
                target_column=TARGET_COLUMN,
                positive_class="1",
                primary_metric="pr_auc_matured",
                metric_rationale="Binary classification stream evaluation",
                operating_threshold=self._operating_threshold,
                label_delay_days=1,
                label_maturity_days=1,
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

        checks = [
            CheckSpec(
                check_id="chk_null_share_salary",
                name="Null share Salary",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_out_of_range_age",
                name="Out of range Age",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_null_share_loan",
                name="Null share Loan",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_null_share_hvalue",
                name="Null share Hvalue",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_out_of_range_loan",
                name="Out of range Loan",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_salary",
                name="Drift Salary",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_loan",
                name="Drift Loan",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_hvalue",
                name="Drift Hvalue",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_commission",
                name="Drift Commission",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_age",
                name="Drift Age",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_pr_auc",
                name="PR-AUC matured",
                category="model_performance",
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
        ]

        operations = Operations(
            scheduler=Scheduler(name="cron", config_location="deploy/crontab"),
            feeds=[
                Feed(
                    name="features_feed",
                    kind="data",
                    ref=DataFeedRef(connection="data", table="features"),
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
                    name="Drift Ops",
                    contacts=[Contact(channel="discord", address="drift_ops")],
                    hours=OnCallHours(
                        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        start="00:00",
                        end="23:59",
                    ),
                    timezone="UTC",
                    escalation_order=1,
                )
            ],
            checks=checks,
            results_location=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
        )

        return HandoffPayload(
            models=[model],
            commands=[],
            data_roles=[
                DataRole(
                    connection="data",
                    table="features",
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
        self._ensure_data_initialized()
        if self._splits is None or self._reference_df is None or self._model is None:
            raise RuntimeError("Drift dataset splits or model failed to initialize")

        data_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=UTC)
        day_df = self._splits[day].copy()

        # Apply active injections for day
        for inj in self._injections:
            day_df = inj.apply(day_df, day)

        # 1. Write features.csv
        file_df = day_df.copy()
        file_df["id"] = list(range(len(file_df)))
        file_df["date"] = [day.isoformat()] * len(file_df)
        cast(Any, file_df).to_csv(data_dir / "features.csv", index=False)

        # 2. Agent checks
        checks_file = results_dir / "checks.jsonl"
        with checks_file.open("a", encoding="utf-8") as f:
            # Data quality checks
            rec_sal_null = null_share(
                day_df,
                "salary",
                ran_at=day_start,
                check_id="chk_null_share_salary",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_sal_null.model_dump_json() + "\n")

            rec_age_range = out_of_range(
                day_df,
                "age",
                ran_at=day_start,
                lower=20.0,
                upper=80.0,
                check_id="chk_out_of_range_age",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_age_range.model_dump_json() + "\n")

            rec_loan_null = null_share(
                day_df,
                "loan",
                ran_at=day_start,
                check_id="chk_null_share_loan",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_loan_null.model_dump_json() + "\n")

            rec_hval_null = null_share(
                day_df,
                "hvalue",
                ran_at=day_start,
                check_id="chk_null_share_hvalue",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_hval_null.model_dump_json() + "\n")

            rec_loan_range = out_of_range(
                day_df,
                "loan",
                ran_at=day_start,
                lower=0.0,
                upper=500000.0,
                check_id="chk_out_of_range_loan",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_loan_range.model_dump_json() + "\n")

            # Drift checks
            for col, chk_id in [
                ("salary", "chk_drift_salary"),
                ("loan", "chk_drift_loan"),
                ("hvalue", "chk_drift_hvalue"),
                ("commission", "chk_drift_commission"),
                ("age", "chk_drift_age"),
            ]:
                rec_drift = psi_drift(
                    day_df,
                    self._reference_df,
                    col,
                    ran_at=day_start,
                    check_id=chk_id,
                    source_ref="results/checks.jsonl",
                )
                f.write(rec_drift.model_dump_json() + "\n")

            # Model performance check
            day_idx = self._business_days.index(day)
            if day_idx >= self._train_days:
                if self._prev_scores is not None and self._prev_y_true is not None:
                    rec_prauc = pr_auc_matured(
                        self._prev_y_true,
                        self._prev_scores,
                        ran_at=day_start,
                        check_id="chk_pr_auc",
                        model="synthetic-drift-model",
                        source_ref="results/checks.jsonl",
                    )
                    f.write(rec_prauc.model_dump_json() + "\n")

                # Compute today's predictions
                X_today = cast(Any, day_df)[FEATURE_COLUMNS]
                raw_preds = cast(Any, self._model.predict_proba(X_today))
                scores = [float(p[1]) for p in raw_preds]
                self._prev_scores = scores
                self._prev_y_true = [int(y) for y in cast(Sequence[Any], day_df[TARGET_COLUMN])]
            else:
                scores = [0.0] * len(day_df)

        # 3. Team baseline check (accuracy)
        if day_idx >= self._train_days:
            y_day = [int(y) for y in cast(Sequence[Any], day_df[TARGET_COLUMN])]
            preds = [1 if s >= self._operating_threshold else 0 for s in scores]
            rec_acc = accuracy(
                y_day,
                preds,
                ran_at=day_start,
                check_id="chk_accuracy",
                model="synthetic-drift-model",
                source_ref="results/team.jsonl",
            )
            team_file = results_dir / "team.jsonl"
            with team_file.open("a", encoding="utf-8") as f:
                f.write(rec_acc.model_dump_json() + "\n")
