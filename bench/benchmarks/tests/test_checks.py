from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
import pytest

from benchmarks.checks import (
    ACCURACY_THRESHOLD,
    NULL_SHARE_THRESHOLD,
    OUT_OF_RANGE_THRESHOLD,
    PR_AUC_THRESHOLD,
    PSI_DRIFT_THRESHOLD,
    accuracy,
    null_share,
    out_of_range,
    pr_auc_matured,
    psi_drift,
)

NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_null_share_normal_and_crossing() -> None:
    normal_df = pd.DataFrame({"col": [1.0, 2.0, 3.0, 4.0, 5.0]})
    rec_normal = null_share(normal_df, "col", ran_at=NOW)
    assert rec_normal.check_id == "null_share"
    assert rec_normal.origin == "agent"
    assert rec_normal.category == "data_quality"
    assert rec_normal.subject == "col"
    assert rec_normal.fired is False
    assert rec_normal.value == 0.0
    assert rec_normal.threshold == NULL_SHARE_THRESHOLD

    crossing_df = pd.DataFrame({"col": [1.0, None, None, None, 5.0]})
    rec_crossing = null_share(crossing_df, "col", ran_at=NOW)
    assert rec_crossing.fired is True
    assert rec_crossing.value == pytest.approx(0.6)
    assert rec_crossing.threshold == NULL_SHARE_THRESHOLD


def test_out_of_range_normal_and_crossing() -> None:
    ref_df = pd.DataFrame({"val": [10.0, 20.0, 30.0, 40.0, 50.0]})
    normal_df = pd.DataFrame({"val": [15.0, 25.0, 35.0]})
    rec_normal = out_of_range(normal_df, "val", ran_at=NOW, reference=ref_df)
    assert rec_normal.origin == "agent"
    assert rec_normal.category == "data_quality"
    assert rec_normal.subject == "val"
    assert rec_normal.fired is False
    assert rec_normal.value == 0.0
    assert rec_normal.threshold == OUT_OF_RANGE_THRESHOLD

    crossing_df = pd.DataFrame({"val": [15.0, 25.0, 100.0]})
    rec_crossing = out_of_range(crossing_df, "val", ran_at=NOW, reference=ref_df)
    assert rec_crossing.fired is True
    assert rec_crossing.value is not None
    assert rec_crossing.value > 0.0

    # Explicit lower/upper
    rec_explicit = out_of_range(crossing_df, "val", ran_at=NOW, lower=0.0, upper=50.0)
    assert rec_explicit.fired is True


def test_psi_drift_normal_and_crossing() -> None:
    ref = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0] * 10)
    normal = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0] * 10)
    rec_normal = psi_drift(normal, ref, "feature", ran_at=NOW)
    assert rec_normal.origin == "agent"
    assert rec_normal.category == "drift"
    assert rec_normal.subject == "feature"
    assert rec_normal.fired is False
    assert rec_normal.value is not None
    assert rec_normal.value < PSI_DRIFT_THRESHOLD

    # Shifted distribution
    crossing = pd.Series([20.0, 21.0, 22.0, 23.0, 24.0, 25.0] * 10)
    rec_crossing = psi_drift(crossing, ref, "feature", ran_at=NOW)
    assert rec_crossing.fired is True
    assert rec_crossing.value is not None
    assert rec_crossing.value >= PSI_DRIFT_THRESHOLD


def test_pr_auc_matured_normal_and_crossing() -> None:
    y_true = [0, 1, 0, 1, 0, 1, 0, 0, 0, 1]
    y_score_good = [0.1, 0.9, 0.2, 0.85, 0.05, 0.95, 0.15, 0.2, 0.1, 0.8]
    rec_normal = pr_auc_matured(y_true, y_score_good, ran_at=NOW)
    assert rec_normal.origin == "agent"
    assert rec_normal.category == "model_performance"
    assert rec_normal.fired is False
    assert rec_normal.value is not None
    assert rec_normal.value >= PR_AUC_THRESHOLD

    # Bad predictions
    y_score_bad = [0.9, 0.1, 0.8, 0.05, 0.95, 0.1, 0.85, 0.7, 0.9, 0.2]
    rec_crossing = pr_auc_matured(y_true, y_score_bad, ran_at=NOW)
    assert rec_crossing.fired is True
    assert rec_crossing.value is not None
    assert rec_crossing.value < PR_AUC_THRESHOLD


def test_accuracy_normal_and_crossing() -> None:
    y_true = [1, 0, 1, 1, 0, 1, 0, 0, 1, 1]
    y_pred_good = [1, 0, 1, 1, 0, 1, 0, 0, 1, 1]
    rec_normal = accuracy(y_true, y_pred_good, ran_at=NOW)
    assert rec_normal.origin == "team"
    assert rec_normal.category == "model_performance"
    assert rec_normal.fired is False
    assert rec_normal.value == 1.0
    assert rec_normal.threshold == ACCURACY_THRESHOLD

    # Bad accuracy
    y_pred_bad = [0, 1, 0, 0, 1, 0, 1, 1, 0, 0]
    rec_crossing = accuracy(y_true, y_pred_bad, ran_at=NOW)
    assert rec_crossing.origin == "team"
    assert rec_crossing.fired is True
    assert rec_crossing.value == 0.0
