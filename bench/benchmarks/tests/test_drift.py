"""Tests for synthetic drift dataset, scenario, injections, and model."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
import pytest
from demo_world.answer_key import append_new, lines
from mlops_agents.api.app import create_app
from mlops_agents.config import RunMode, load_settings

from benchmarks.client import in_process
from benchmarks.datasets.contract import Dataset
from benchmarks.datasets.drift import (
    DEFAULT_SCENARIO_PATH,
    FEATURE_COLUMNS,
    TARGET_COLUMN,
    CovariateShiftInjection,
    DriftDataset,
    generate_drift_frame,
    load_drift_scenario,
)
from benchmarks.replay import replay


def test_drift_dataset_implements_protocol() -> None:
    ds = DriftDataset(seed=42)
    dataset_protocol: Dataset = ds
    assert dataset_protocol.name == "drift"
    assert dataset_protocol.scenario == "drift"
    assert len(dataset_protocol.days()) == 90
    assert callable(ds.handoff)
    assert callable(ds.prepare)
    assert callable(ds.write_day)
    assert callable(ds.key)


def test_same_seed_gives_same_rows_and_key() -> None:
    ds1 = DriftDataset(seed=42)
    ds2 = DriftDataset(seed=42)

    # Answer keys must match
    assert ds1.key() == ds2.key()

    # Generated frames must match row-for-row with same seed
    df1 = generate_drift_frame(1000, seed=42)
    df2 = generate_drift_frame(1000, seed=42)
    df_diff = generate_drift_frame(1000, seed=99)

    pd.testing.assert_frame_equal(df1, df2)
    assert not df1.equals(df_diff)


def test_drift_point_changes_distribution_only_from_position_on() -> None:
    """Verifies that a drift point changes the distribution only from its position on."""
    # Base stream without drift
    base_df = generate_drift_frame(2000, seed=42)
    # Stream with drift point at row 1000 to function 2
    drift_df = generate_drift_frame(2000, seed=42, position=1000, width=100, drift_function=2)

    # Before position (rows 0 to 800, well before transition width): exact same rows
    base_iloc = cast(Any, base_df.iloc)
    drift_iloc = cast(Any, drift_df.iloc)
    pd.testing.assert_frame_equal(
        cast(pd.DataFrame, base_iloc[:800]),
        cast(pd.DataFrame, drift_iloc[:800]),
    )

    # After position (rows 1000 to 2000): distributions differ
    after_base = cast(pd.DataFrame, base_iloc[1000:2000])
    after_drift = cast(pd.DataFrame, drift_iloc[1000:2000])

    # Target class distribution or model prediction differs
    base_targets = [float(v) for v in cast(Sequence[Any], after_base[TARGET_COLUMN])]
    drift_targets = [float(v) for v in cast(Sequence[Any], after_drift[TARGET_COLUMN])]
    base_target_mean = float(np.mean(base_targets))
    drift_target_mean = float(np.mean(drift_targets))
    assert abs(base_target_mean - drift_target_mean) > 0.1

    # Also test covariate shift injection on a frame
    sample_df = generate_drift_frame(1000, seed=42)
    inj = CovariateShiftInjection(
        column="salary",
        shift=50000.0,
        start=date(2026, 2, 9),
        end=date(2026, 2, 11),
    )
    # On inactive day: completely unchanged
    clean_df = inj.apply(sample_df, day=date(2026, 2, 8))
    pd.testing.assert_frame_equal(sample_df, clean_df)

    # On active day: salary shifted by 50000.0, other columns unchanged
    shifted_df = inj.apply(sample_df, day=date(2026, 2, 9))
    shifted_salary = [float(v) for v in cast(Sequence[Any], shifted_df["salary"])]
    sample_salary = [float(v) for v in cast(Sequence[Any], sample_df["salary"])]
    assert np.allclose(shifted_salary, np.array(sample_salary) + 50000.0)
    for col in FEATURE_COLUMNS:
        if col != "salary":
            pd.testing.assert_series_equal(
                cast(pd.Series, shifted_df[col]),
                cast(pd.Series, sample_df[col]),
            )


def test_scenario_lines_appended_once(tmp_path: Path) -> None:
    ds = DriftDataset(seed=42)
    key_records = ds.key()
    assert len(key_records) >= 15

    # First append
    append_new(tmp_path, key_records)
    recorded_lines = lines(tmp_path)
    assert len(recorded_lines) == len(key_records)

    # Second append must be idempotent: no new lines added
    append_new(tmp_path, key_records)
    recorded_lines_2 = lines(tmp_path)
    assert len(recorded_lines_2) == len(key_records)


def test_spacing_and_problem_counts_in_drift_scenario() -> None:
    assert DEFAULT_SCENARIO_PATH.exists(), f"Scenario file not found at {DEFAULT_SCENARIO_PATH}"
    injections = load_drift_scenario(DEFAULT_SCENARIO_PATH)

    dq_problems = [inj for inj in injections if inj.category == "data_quality"]
    drift_problems = [inj for inj in injections if inj.category == "drift"]
    perf_problems = [inj for inj in injections if inj.category == "model_performance"]

    assert len(dq_problems) >= 5, f"Expected >= 5 data_quality, got {len(dq_problems)}"
    assert len(drift_problems) >= 5, f"Expected >= 5 drift, got {len(drift_problems)}"
    assert len(perf_problems) >= 5, f"Expected >= 5 model_performance, got {len(perf_problems)}"

    first_scored = date(2026, 2, 2)  # Day 21
    last_scored = date(2026, 5, 8)  # Day 90

    for inj in injections:
        assert inj.start >= first_scored, (
            f"{inj} starts before day 21 ({inj.start} < {first_scored})"
        )
        assert inj.end <= last_scored, f"{inj} ends after day 90 ({inj.end} > {last_scored})"

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
                f"{cat_name}: match span of {p1} ends {span1_end}, overlaps {p2} start {span2_start}"
            )


@pytest.mark.asyncio
async def test_short_replay_opens_drift_incident_scored_real_issue(tmp_path: Path) -> None:
    """A short replay (a few days, one drift point) through replay opens a drift incident scored real_issue."""
    run_dir = tmp_path / "short_replay"

    test_days = (
        date(2026, 2, 2),
        date(2026, 2, 3),
        date(2026, 2, 4),
        date(2026, 2, 5),
    )
    drift_injection = CovariateShiftInjection(
        column="salary",
        shift=60000.0,
        start=date(2026, 2, 4),
        end=date(2026, 2, 5),
    )

    dataset = DriftDataset(
        days=test_days,
        injections=[drift_injection],
        seed=42,
        samples_per_day=300,
        train_days=2,
    )

    score_res = await replay(dataset, run_dir)

    assert run_dir.joinpath("report.md").exists()
    assert run_dir.joinpath("report.json").exists()

    # Read incidents directly from the API
    settings = load_settings(
        RunMode.LOCAL,
        port=8765,
        env={
            "MLOPS_AGENTS_HOME": str(run_dir / "home"),
            "MLOPS_AGENTS_ACCESS_TOKEN": "test-token",
        },
    )
    app = create_app(settings)
    app.state.access_token = "test-token"
    async with app.router.lifespan_context(app):
        services = app.state.services
        projects = await services.workspace.list_projects()
        assert len(projects) == 1
        proj_id = projects[0].id
        client = in_process(app, "test-token")
        inc_page = await client.get(f"/projects/{proj_id}/incidents")
        inc_rows = inc_page["rows"]
        assert len(inc_rows) >= 1
        drift_incs = [r for r in inc_rows if r["category"] == "drift"]
        assert len(drift_incs) >= 1

    # Verify score results
    assert len(score_res.matched) >= 1
    drift_matched = [
        (inc_id, prob) for inc_id, prob in score_res.matched.items() if prob.category == "drift"
    ]
    assert len(drift_matched) >= 1
    drift_inc_id, drift_problem = drift_matched[0]
    assert drift_problem.kind == "covariate_shift"
    assert score_res.verdicts[drift_inc_id].value == "real_issue"
