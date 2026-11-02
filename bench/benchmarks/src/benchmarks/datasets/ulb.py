"""ULB credit card fraud dataset, injections, and LightGBM model."""

from __future__ import annotations

import hashlib
import tomllib
import urllib.request
from collections.abc import Sequence
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

from benchmarks.checks import (
    accuracy,
    null_share,
    out_of_range,
    pr_auc_matured,
    psi_drift,
)

OPENML_1597_URL: Final[str] = "https://data.openml.org/datasets/0000/1597/dataset_1597.pq"
OPENML_1597_SHA256: Final[str] = "b7efcb35a428bbe22347a05d2437d9177bab07ce61e51214a17bec584ad9496d"
OPENML_1597_FILE: Final[str] = "dataset_1597.pq"

FEATURE_COLUMNS: Final[list[str]] = [f"V{i}" for i in range(1, 29)] + ["Amount"]
TARGET_COLUMN: Final[str] = "Class"

DEFAULT_SCENARIO_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent.parent / "scenarios" / "ulb.toml"
)

_CHUNK: Final[int] = 1 << 20
_TIMEOUT_SECONDS: Final[int] = 120


class FetchError(Exception):
    """The dataset file could not be downloaded or failed hash verification."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_ulb_parquet(
    dest_dir: Path,
    url: str = OPENML_1597_URL,
    sha256: str = OPENML_1597_SHA256,
) -> Path:
    """Downloads OpenML 1597 parquet, verifying its SHA-256."""
    target = dest_dir / OPENML_1597_FILE
    if target.exists():
        if _sha256(target) == sha256:
            return target
        target.unlink()

    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = dest_dir / f"{OPENML_1597_FILE}.part"
    digest = hashlib.sha256()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"URL scheme must be http or https, got {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "benchmarks/0.1.0"})  # noqa: S310
    try:
        with (
            urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response,  # noqa: S310
            partial.open("wb") as out,
        ):
            while chunk := response.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
    except BaseException as error:
        partial.unlink(missing_ok=True)
        if isinstance(error, Exception):
            raise FetchError(f"Downloading {url} failed: {error!r}") from error
        raise

    actual = digest.hexdigest()
    if actual != sha256:
        partial.unlink()
        raise FetchError(f"{url} has SHA-256 {actual}, expected {sha256}; nothing was kept")
    partial.replace(target)
    return target


def ulb_business_days(start_date: date = date(2026, 1, 5), count: int = 90) -> list[date]:
    """Generates `count` business days starting from `start_date`."""
    days: list[date] = []
    current = start_date
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def split_ulb_days(df: pd.DataFrame, days: Sequence[date]) -> dict[date, pd.DataFrame]:
    """Sorts DataFrame by Time and splits evenly into given business days."""
    sorted_df = df.sort_values(by="Time", kind="stable").reset_index(drop=True)
    n_rows = len(sorted_df)
    n_days = len(days)
    splits = np.array_split(np.arange(n_rows), n_days)
    result: dict[date, pd.DataFrame] = {}
    iloc_obj = cast(Any, sorted_df.iloc)
    for d, indices in zip(days, splits, strict=True):
        subset = cast(pd.DataFrame, iloc_obj[indices].copy().reset_index(drop=True))
        result[d] = subset
    return result


@dataclass(frozen=True, kw_only=True)
class ULBInjection:
    """Base class for scenario injections on ULB credit card fraud data."""

    kind: str
    category: Category
    column: str | None
    start: date
    end: date

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
class NullSpikeInjection(ULBInjection):
    """Spikes missing values in a given column for active days."""

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

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or self.column is None:
            return df
        if len(df) == 0:
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
class OutOfRangeInjection(ULBInjection):
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

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or self.column is None:
            return df
        if len(df) == 0:
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
class CovariateShiftInjection(ULBInjection):
    """Shifts values in a column by adding an offset for active days."""

    shift: float

    def __init__(
        self,
        *,
        column: str,
        shift: float,
        start: date,
        end: date,
    ) -> None:
        object.__setattr__(self, "kind", "covariate_shift")
        object.__setattr__(self, "category", "drift")
        object.__setattr__(self, "column", column)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "shift", shift)

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day) or self.column is None:
            return df
        if len(df) == 0:
            return df
        result = df.copy()
        vals = [float(v) + self.shift for v in cast(Sequence[Any], result[self.column])]
        result[self.column] = vals
        return result


@dataclass(frozen=True, kw_only=True)
class UnseenFraudInjection(ULBInjection):
    """Introduces an unseen fraud pattern (flips low-risk normal transactions to fraud)."""

    share: float

    def __init__(
        self,
        *,
        share: float,
        start: date,
        end: date,
    ) -> None:
        object.__setattr__(self, "kind", "unseen_fraud")
        object.__setattr__(self, "category", "model_performance")
        object.__setattr__(self, "column", None)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "share", share)

    def apply(self, df: pd.DataFrame, day: date) -> pd.DataFrame:
        if not self.active(day):
            return df
        if len(df) == 0:
            return df
        result = df.copy()
        classes = [int(v) for v in cast(Sequence[Any], result[TARGET_COLUMN])]
        non_fraud_indices = [i for i, c in enumerate(classes) if c == 0]
        if not non_fraud_indices:
            return result
        flip_count = max(1, round(self.share * len(non_fraud_indices)))
        for idx in non_fraud_indices[:flip_count]:
            classes[idx] = 1
        result[TARGET_COLUMN] = classes
        return result


def load_ulb_scenario(path: Path) -> list[ULBInjection]:
    """Loads injection definitions from a scenario TOML file."""
    with path.open("rb") as f:
        data = tomllib.load(f)

    injections: list[ULBInjection] = []
    raw_injections = data.get("injection", [])
    for item in raw_injections:
        kind = str(item.get("kind", ""))
        start_raw = str(item.get("start", ""))
        end_raw = str(item.get("end", ""))
        start_d = date.fromisoformat(start_raw)
        end_d = date.fromisoformat(end_raw)

        if kind == "null_spike":
            col = str(item.get("column", "V1"))
            share = float(item.get("share", 0.2))
            injections.append(NullSpikeInjection(column=col, share=share, start=start_d, end=end_d))
        elif kind == "out_of_range":
            col = str(item.get("column", "Amount"))
            val = float(item.get("value", -100.0))
            share = float(item.get("share", 0.2))
            injections.append(
                OutOfRangeInjection(column=col, value=val, share=share, start=start_d, end=end_d)
            )
        elif kind == "covariate_shift":
            col = str(item.get("column", "Amount"))
            shift = float(item.get("shift", 500.0))
            injections.append(
                CovariateShiftInjection(column=col, shift=shift, start=start_d, end=end_d)
            )
        elif kind == "unseen_fraud":
            share = float(item.get("share", 0.3))
            injections.append(UnseenFraudInjection(share=share, start=start_d, end=end_d))
        else:
            raise ValueError(f"Unknown or unsupported injection kind: {kind!r}")
    return injections


def train_ulb_model(train_df: pd.DataFrame) -> lgb.LGBMClassifier:
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


class ULBDataset:
    """ULB credit card fraud benchmark dataset implementation."""

    name: str = "ulb"

    def __init__(
        self,
        *,
        frame: pd.DataFrame | None = None,
        scenario_path: Path | None = None,
        injections: Sequence[ULBInjection] | None = None,
    ) -> None:
        self._frame = frame
        self._scenario_path = scenario_path or DEFAULT_SCENARIO_PATH
        if injections is not None:
            self._injections = list(injections)
        elif self._scenario_path.exists():
            self._injections = load_ulb_scenario(self._scenario_path)
        else:
            self._injections = []

        self._business_days = ulb_business_days(date(2026, 1, 5), count=90)
        self._cache_dir: Path | None = None
        self._splits: dict[date, pd.DataFrame] | None = None
        self._model: lgb.LGBMClassifier | None = None
        self._reference_df: pd.DataFrame | None = None
        self._prev_scores: list[float] | None = None
        self._prev_y_true: list[int] | None = None

    @property
    def injections(self) -> list[ULBInjection]:
        return list(self._injections)

    @property
    def cache_dir(self) -> Path | None:
        return self._cache_dir

    def days(self) -> Sequence[date]:
        return self._business_days

    def key(self) -> Sequence[Record]:
        return [inj.record() for inj in self._injections]

    def prepare(self, cache: Path) -> None:
        self._cache_dir = cache
        if self._frame is None:
            fetch_ulb_parquet(cache)

    def _ensure_data_initialized(self, cache: Path | None = None) -> None:
        if self._splits is not None:
            return

        if self._frame is not None:
            raw_df = self._frame.copy()
        else:
            cache_dir = cache or self._cache_dir or Path.home() / "bench-home" / "cache"
            pq_path = cache_dir / OPENML_1597_FILE
            if not pq_path.exists():
                pq_path = fetch_ulb_parquet(cache_dir)
            raw_df = cast(pd.DataFrame, cast(Any, pd).read_parquet(pq_path))

        # Normalize Class column to int
        if TARGET_COLUMN in raw_df.columns:
            raw_df[TARGET_COLUMN] = [
                int(str(v).strip("'\"")) for v in cast(Sequence[Any], raw_df[TARGET_COLUMN])
            ]

        self._splits = split_ulb_days(raw_df, self._business_days)

        # Train model on first 20 days
        train_days = self._business_days[:20]
        train_df = pd.concat([self._splits[d] for d in train_days], ignore_index=True)
        self._reference_df = train_df
        self._model = train_ulb_model(train_df)

    def handoff(self) -> HandoffPayload:
        model = ModelSpec(
            name="ulb-creditcard-model",
            profile=Profile(
                objective="Detect credit card fraud under extreme class imbalance",
                problem_type="binary_classification",
                target_column=TARGET_COLUMN,
                positive_class="1",
                primary_metric="pr_auc_matured",
                metric_rationale="Extreme class imbalance (<0.2% fraud)",
                operating_threshold=0.5,
                label_delay_days=1,
                label_maturity_days=1,
                serving_mode="batch",
                error_costs="Asymmetric",
                cost_ratio=100.0,
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
                check_id="chk_null_share_v1",
                name="Null share V1",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_out_of_range_amount",
                name="Out of range Amount",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_null_share_amount",
                name="Null share Amount",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_null_share_v3",
                name="Null share V3",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_amount",
                name="Drift Amount",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_v1",
                name="Drift V1",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_v2",
                name="Drift V2",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_v4",
                name="Drift V4",
                category="drift",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="data", path_pattern="results/checks*.jsonl"),
            ),
            CheckSpec(
                check_id="chk_drift_v5",
                name="Drift V5",
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
                    name="transactions_feed",
                    kind="data",
                    ref=DataFeedRef(connection="data", table="transactions"),
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
                    name="ULB Ops",
                    contacts=[Contact(channel="discord", address="u_ops")],
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
                    table="transactions",
                    role="training",
                    id_column="Time",
                    time_column="Time",
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
            raise RuntimeError("ULB dataset splits or model failed to initialize")

        data_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=UTC)
        day_df = self._splits[day].copy()

        # Apply active injections for day
        for inj in self._injections:
            day_df = inj.apply(day_df, day)

        # 1. Write transactions.csv
        cast(Any, day_df).to_csv(data_dir / "transactions.csv", index=False)

        # 2. Agent checks
        checks_file = results_dir / "checks.jsonl"
        with checks_file.open("a", encoding="utf-8") as f:
            # Data quality checks
            rec_v1 = null_share(
                day_df,
                "V1",
                ran_at=day_start,
                check_id="chk_null_share_v1",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_v1.model_dump_json() + "\n")

            rec_amt_null = null_share(
                day_df,
                "Amount",
                ran_at=day_start,
                check_id="chk_null_share_amount",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_amt_null.model_dump_json() + "\n")

            rec_v3_null = null_share(
                day_df,
                "V3",
                ran_at=day_start,
                check_id="chk_null_share_v3",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_v3_null.model_dump_json() + "\n")

            rec_amt_range = out_of_range(
                day_df,
                "Amount",
                ran_at=day_start,
                lower=0.0,
                check_id="chk_out_of_range_amount",
                source_ref="results/checks.jsonl",
            )
            f.write(rec_amt_range.model_dump_json() + "\n")

            # Drift checks
            for col, chk_id in [
                ("Amount", "chk_drift_amount"),
                ("V1", "chk_drift_v1"),
                ("V2", "chk_drift_v2"),
                ("V4", "chk_drift_v4"),
                ("V5", "chk_drift_v5"),
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
            # Evaluate matured labels from previous scored day if available
            day_idx = self._business_days.index(day)
            if day_idx >= 20:
                if self._prev_scores is not None and self._prev_y_true is not None:
                    rec_prauc = pr_auc_matured(
                        self._prev_y_true,
                        self._prev_scores,
                        ran_at=day_start,
                        check_id="chk_pr_auc",
                        model="ulb-creditcard-model",
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
        y_day = [int(y) for y in cast(Sequence[Any], day_df[TARGET_COLUMN])]
        preds = [1 if s >= 0.5 else 0 for s in scores]
        rec_acc = accuracy(
            y_day,
            preds,
            ran_at=day_start,
            check_id="chk_accuracy",
            model="ulb-creditcard-model",
            source_ref="results/team.jsonl",
        )
        team_file = results_dir / "team.jsonl"
        with team_file.open("a", encoding="utf-8") as f:
            f.write(rec_acc.model_dump_json() + "\n")
