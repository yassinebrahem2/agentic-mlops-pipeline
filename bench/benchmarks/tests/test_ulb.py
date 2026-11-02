"""Tests for ULB credit card fraud dataset, injections, scenario, and model."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import numpy as np
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
import pytest
from demo_world.answer_key import Record

from benchmarks.checks import pr_auc_matured
from benchmarks.datasets.contract import Dataset
from benchmarks.datasets.ulb import (
    FEATURE_COLUMNS,
    OPENML_1597_SHA256,
    OPENML_1597_URL,
    CovariateShiftInjection,
    FetchError,
    NullSpikeInjection,
    OutOfRangeInjection,
    ULBDataset,
    UnseenFraudInjection,
    fetch_ulb_parquet,
    load_ulb_scenario,
    split_ulb_days,
    train_ulb_model,
    ulb_business_days,
)


def _make_synthetic_ulb_df(n_rows: int = 180) -> pd.DataFrame:
    """Creates a deterministic synthetic DataFrame matching ULB creditcard schema."""
    rng = np.random.default_rng(42)
    times = [float(t) for t in np.sort(rng.uniform(0.0, 172800.0, size=n_rows))]
    data: dict[str, Any] = {"Time": times}
    for i in range(1, 29):
        data[f"V{i}"] = [float(v) for v in rng.normal(0.0, 1.0, size=n_rows)]
    data["Amount"] = [float(a) for a in rng.uniform(1.0, 200.0, size=n_rows)]
    # Binary class: mostly 0, few 1s
    classes = [0] * n_rows
    for idx in [5, 15, 50, 100]:
        if idx < n_rows:
            classes[idx] = 1
    data["Class"] = classes
    return pd.DataFrame(data)


def test_ulb_dataset_implements_protocol() -> None:
    synthetic = _make_synthetic_ulb_df(180)
    ds = ULBDataset(frame=synthetic)
    dataset_protocol: Dataset = ds
    assert dataset_protocol.name == "ulb"
    assert len(dataset_protocol.days()) == 90
    assert callable(ds.handoff)
    assert callable(ds.prepare)
    assert callable(ds.write_day)
    assert callable(ds.key)


def test_openml_constants() -> None:
    assert "openml.org" in OPENML_1597_URL
    assert OPENML_1597_SHA256 == "b7efcb35a428bbe22347a05d2437d9177bab07ce61e51214a17bec584ad9496d"


def test_day_split_keeps_every_row_once_in_time_order() -> None:
    n_rows = 270
    df = _make_synthetic_ulb_df(n_rows)
    days = ulb_business_days(start_date=date(2026, 1, 5), count=90)
    splits = split_ulb_days(df, days)

    assert len(splits) == 90
    total_split_rows = sum(len(split_df) for split_df in splits.values())
    assert total_split_rows == n_rows

    # Verify rows concatenated back match df exactly in Time order
    reconstructed = pd.concat([splits[d] for d in days], ignore_index=True)
    assert len(reconstructed) == n_rows

    reconstructed_times = [float(t) for t in cast(Sequence[Any], reconstructed["Time"])]
    df_times = [float(t) for t in cast(Sequence[Any], df["Time"])]
    assert reconstructed_times == df_times

    reconstructed_classes = [int(c) for c in cast(Sequence[Any], reconstructed["Class"])]
    df_classes = [int(c) for c in cast(Sequence[Any], df["Class"])]
    assert reconstructed_classes == df_classes
    assert all(t2 >= t1 for t1, t2 in itertools.pairwise(reconstructed_times))


def test_no_two_problems_of_one_category_have_overlapping_match_spans() -> None:
    scenario_path = Path(__file__).resolve().parent.parent / "scenarios" / "ulb.toml"
    assert scenario_path.exists(), f"Scenario file not found at {scenario_path}"

    injections = load_ulb_scenario(scenario_path)

    dq_problems = [inj for inj in injections if inj.category == "data_quality"]
    drift_problems = [inj for inj in injections if inj.category == "drift"]
    perf_problems = [inj for inj in injections if inj.category == "model_performance"]

    assert len(dq_problems) >= 5, f"Expected >= 5 data_quality, got {len(dq_problems)}"
    assert len(drift_problems) >= 5, f"Expected >= 5 drift, got {len(drift_problems)}"
    assert len(perf_problems) >= 1, f"Expected >= 1 model_performance, got {len(perf_problems)}"

    first_scored = date(2026, 2, 2)
    last_scored = date(2026, 5, 8)

    for inj in injections:
        assert inj.start >= first_scored, f"{inj} starts before day 21"
        assert inj.end <= last_scored, f"{inj} ends after day 90"
        cur = inj.start
        b_count = 0
        while cur <= inj.end:
            if cur.weekday() < 5:
                b_count += 1
            cur += timedelta(days=1)
        assert b_count == 3, f"{inj} does not span 3 business days: {b_count}"

    for cat_name, prob_list in [
        ("data_quality", dq_problems),
        ("drift", drift_problems),
        ("model_performance", perf_problems),
    ]:
        sorted_probs = sorted(prob_list, key=lambda x: x.start)
        for i in range(len(sorted_probs) - 1):
            p1 = sorted_probs[i]
            p2 = sorted_probs[i + 1]
            gap = (p2.start - p1.end).days
            assert gap >= 9, (
                f"{cat_name}: {p2.start} starts only {gap} days after {p1.end} (expected >= 9)"
            )

            span1_end = p1.end + timedelta(days=7)
            span2_start = p2.start - timedelta(days=1)
            assert span1_end < span2_start, (
                f"{cat_name}: overlapping match spans: {p1} span end {span1_end} >= "
                f"{p2} span start {span2_start}"
            )


def test_scenario_injection_changes_only_window_days_and_appends_record() -> None:
    df = _make_synthetic_ulb_df(50)
    start_d = date(2026, 2, 2)
    end_d = date(2026, 2, 4)
    outside_d = date(2026, 2, 5)

    # 1. NullSpikeInjection
    ns = NullSpikeInjection(column="V1", share=0.3, start=start_d, end=end_d)
    assert not ns.active(outside_d)
    assert ns.active(start_d)
    assert ns.active(date(2026, 2, 3))
    assert ns.active(end_d)

    # Outside window: df untouched
    out_df = ns.apply(df, outside_d)
    out_v1 = [v for v in cast(Sequence[Any], out_df["V1"]) if isinstance(v, float) and np.isnan(v)]
    assert len(out_v1) == 0

    # Inside window: fraction of V1 are null
    in_df = ns.apply(df, start_d)
    in_v1 = [v for v in cast(Sequence[Any], in_df["V1"]) if isinstance(v, float) and np.isnan(v)]
    assert len(in_v1) > 0
    rec = ns.record()
    assert rec == Record(
        kind="null_spike",
        category="data_quality",
        column="V1",
        start="2026-02-02",
        end="2026-02-04",
    )

    # 2. OutOfRangeInjection
    oor = OutOfRangeInjection(column="Amount", value=-99.0, share=0.2, start=start_d, end=end_d)
    oor_out_amt = [float(v) for v in cast(Sequence[Any], oor.apply(df, outside_d)["Amount"])]
    assert min(oor_out_amt) >= 0.0
    oor_in_amt = [float(v) for v in cast(Sequence[Any], oor.apply(df, start_d)["Amount"])]
    assert any(v < 0.0 for v in oor_in_amt)
    assert oor.record().category == "data_quality"
    assert oor.record().kind == "out_of_range"

    # 3. CovariateShiftInjection
    cs = CovariateShiftInjection(column="Amount", shift=500.0, start=start_d, end=end_d)
    cs_out_amt = [float(v) for v in cast(Sequence[Any], cs.apply(df, outside_d)["Amount"])]
    assert max(cs_out_amt) < 300.0
    cs_in_amt = [float(v) for v in cast(Sequence[Any], cs.apply(df, start_d)["Amount"])]
    assert max(cs_in_amt) > 500.0
    assert cs.record().category == "drift"
    assert cs.record().kind == "covariate_shift"

    # 4. UnseenFraudInjection
    uf = UnseenFraudInjection(share=0.4, start=start_d, end=end_d)
    uf_out = sum(int(v) for v in cast(Sequence[Any], uf.apply(df, outside_d)["Class"]))
    uf_in = sum(int(v) for v in cast(Sequence[Any], uf.apply(df, start_d)["Class"]))
    base_sum = sum(int(v) for v in cast(Sequence[Any], df["Class"]))
    assert uf_out == base_sum
    assert uf_in > base_sum
    assert uf.record().category == "model_performance"
    assert uf.record().kind == "unseen_fraud"


def test_model_trains_and_scores_on_synthetic_frame() -> None:
    df = _make_synthetic_ulb_df(180)
    days = ulb_business_days(start_date=date(2026, 1, 5), count=90)
    splits = split_ulb_days(df, days)

    train_days = days[:20]
    train_df = pd.concat([splits[d] for d in train_days], ignore_index=True)

    # Train model
    model = train_ulb_model(train_df)
    assert model is not None

    # Score each later day (21 to 90)
    for day in days[20:]:
        day_df = splits[day]
        X_day = cast(Any, day_df[FEATURE_COLUMNS])
        raw_scores = cast(Any, model.predict_proba(X_day))
        scores = [float(s[1]) for s in raw_scores]
        assert len(scores) == len(day_df)
        assert all(0.0 <= s <= 1.0 for s in scores)

    # Evaluate pr_auc_matured
    y_true = [int(v) for v in cast(Sequence[Any], train_df["Class"])]
    raw_train_scores = cast(Any, model.predict_proba(cast(Any, train_df[FEATURE_COLUMNS])))
    y_score = [float(s[1]) for s in raw_train_scores]
    rec = pr_auc_matured(y_true, y_score, ran_at=datetime.now(UTC))
    assert rec.category == "model_performance"
    assert rec.value is not None
    assert rec.value >= 0.0


def test_fetch_refuses_file_whose_hash_differs(tmp_path: Path) -> None:
    fake_content = b"corrupted content"
    wrong_sha256 = hashlib.sha256(b"something else").hexdigest()

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self, chunk_size: int = -1) -> bytes:
            nonlocal fake_content
            data = fake_content
            fake_content = b""
            return data

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(FetchError) as exc_info:
            fetch_ulb_parquet(tmp_path, url="http://example.com/test.pq", sha256=wrong_sha256)
        assert "SHA-256" in str(exc_info.value)
        # Verify no corrupted file left behind
        assert not (tmp_path / "dataset_1597.pq").exists()
        assert not (tmp_path / "dataset_1597.pq.part").exists()


def test_ulb_write_day_writes_data_and_check_results(tmp_path: Path) -> None:
    synthetic = _make_synthetic_ulb_df(180)
    ds = ULBDataset(frame=synthetic)

    data_dir = tmp_path / "data"
    results_dir = data_dir / "results"

    days = ds.days()
    # Write day 1 (training day)
    ds.write_day(days[0], data_dir, results_dir)
    assert (data_dir / "transactions.csv").exists()
    assert (results_dir / "checks.jsonl").exists()
    assert (results_dir / "team.jsonl").exists()

    # Write day 21 (first scored day)
    ds.write_day(days[20], data_dir, results_dir)

    # Write day 22 (matured labels day)
    ds.write_day(days[21], data_dir, results_dir)

    # Check key() returns all answer key records
    key_records = ds.key()
    assert len(key_records) >= 11
    assert any(r.category == "data_quality" for r in key_records)
    assert any(r.category == "drift" for r in key_records)
    assert any(r.category == "model_performance" for r in key_records)


def test_injections_handle_empty_dataframe() -> None:
    cols = ["Time", "Amount", "Class", "V1", "V2", "V3", "V4"]
    empty_df = pd.DataFrame(columns=cols)
    d = date(2026, 2, 2)

    ns = NullSpikeInjection(column="V1", share=0.2, start=d, end=d)
    oor = OutOfRangeInjection(column="Amount", value=-50.0, share=0.2, start=d, end=d)
    cs = CovariateShiftInjection(column="Amount", shift=100.0, start=d, end=d)
    uf = UnseenFraudInjection(share=0.2, start=d, end=d)

    assert len(ns.apply(empty_df, d)) == 0
    assert len(oor.apply(empty_df, d)) == 0
    assert len(cs.apply(empty_df, d)) == 0
    assert len(uf.apply(empty_df, d)) == 0


def test_load_ulb_scenario_raises_on_unknown_kind(tmp_path: Path) -> None:
    toml_path = tmp_path / "bad_scenario.toml"
    toml_path.write_text(
        """
[[injection]]
kind = "unknown_problem"
start = "2026-02-02"
end = "2026-02-04"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unknown or unsupported injection kind"):
        load_ulb_scenario(toml_path)


def test_ulb_dataset_retains_cache_dir(tmp_path: Path) -> None:
    custom_cache = tmp_path / "custom_cache"
    custom_cache.mkdir()

    # Create dummy parquet in custom cache
    synthetic = _make_synthetic_ulb_df(180)
    pq_path = custom_cache / "dataset_1597.pq"
    cast(Any, synthetic).to_parquet(pq_path)

    ds = ULBDataset()
    with patch("benchmarks.datasets.ulb.fetch_ulb_parquet") as mock_fetch:
        ds.prepare(custom_cache)
        mock_fetch.assert_called_once_with(custom_cache)

    assert ds.cache_dir == custom_cache

    # write_day should read from custom_cache without re-downloading
    data_dir = tmp_path / "data"
    results_dir = data_dir / "results"
    with patch("benchmarks.datasets.ulb.fetch_ulb_parquet") as mock_fetch2:
        ds.write_day(ds.days()[0], data_dir, results_dir)
        mock_fetch2.assert_not_called()

    assert (data_dir / "transactions.csv").exists()
    assert (results_dir / "checks.jsonl").exists()
