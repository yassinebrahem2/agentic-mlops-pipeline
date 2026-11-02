"""Fixed generic checks for benchmarks."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final, cast

import numpy as np
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
from mlops_agents.monitoring.results.contract import ResultRecord

NULL_SHARE_THRESHOLD: Final[float] = 0.05
OUT_OF_RANGE_THRESHOLD: Final[float] = 0.0
PSI_DRIFT_THRESHOLD: Final[float] = 0.2
PR_AUC_THRESHOLD: Final[float] = 0.6
ACCURACY_THRESHOLD: Final[float] = 0.9


def _extract_values(
    data: pd.DataFrame | pd.Series | Sequence[Any],
    column: str | None = None,
) -> tuple[list[Any], str]:
    if isinstance(data, pd.DataFrame):
        if column is None:
            if len(data.columns) == 1:
                col_name = str(data.columns[0])
                col_data = cast(Any, data[col_name])
                vals = [x for x in col_data]
                return vals, col_name
            raise ValueError("column must be specified for a multi-column DataFrame")
        col_data = cast(Any, data[column])
        vals = [x for x in col_data]
        return vals, str(column)
    if isinstance(data, pd.Series):
        subj = str(column or data.name or "value")
        vals = [x for x in data]
        return vals, subj
    subj = str(column or "value")
    return list(data), subj


def _is_null(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def null_share(
    current: pd.DataFrame | pd.Series | Sequence[Any],
    column: str | None = None,
    *,
    ran_at: datetime,
    check_id: str = "null_share",
    threshold: float = NULL_SHARE_THRESHOLD,
    model: str | None = None,
    source_ref: str = "checks/checks.jsonl",
) -> ResultRecord:
    """Checks the fraction of null values in a column or series."""
    values, subject = _extract_values(current, column)
    total = len(values)
    if total == 0:
        return ResultRecord(
            check_id=check_id,
            origin="agent",
            ran_at=ran_at,
            fired=False,
            value=None,
            threshold=threshold,
            category="data_quality",
            subject=subject,
            model=model,
            detail={"total": 0, "nulls": 0},
            source_ref=source_ref,
            not_enough_data=True,
        )

    null_count = sum(1 for v in values if _is_null(v))
    share = round(float(null_count / total), 4)
    fired = share > threshold

    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at,
        fired=fired,
        value=share,
        threshold=threshold,
        category="data_quality",
        subject=subject,
        model=model,
        detail={"total": total, "nulls": null_count, "null_share": share},
        source_ref=source_ref,
        not_enough_data=False,
    )


def out_of_range(
    current: pd.DataFrame | pd.Series | Sequence[Any],
    column: str | None = None,
    *,
    ran_at: datetime,
    reference: pd.DataFrame | pd.Series | Sequence[Any] | None = None,
    lower: float | None = None,
    upper: float | None = None,
    check_id: str = "out_of_range",
    threshold: float = OUT_OF_RANGE_THRESHOLD,
    model: str | None = None,
    source_ref: str = "checks/checks.jsonl",
) -> ResultRecord:
    """Checks for values outside a reference range or explicit bounds."""
    values, subject = _extract_values(current, column)
    total = len(values)

    eff_lower = lower
    eff_upper = upper
    if reference is not None:
        ref_values, _ = _extract_values(reference, column)
        clean_ref = [float(v) for v in ref_values if not _is_null(v)]
        if len(clean_ref) > 0:
            if eff_lower is None:
                eff_lower = min(clean_ref)
            if eff_upper is None:
                eff_upper = max(clean_ref)

    if eff_lower is None and eff_upper is None:
        raise ValueError("out_of_range requires reference data or lower/upper bounds")

    if total == 0:
        return ResultRecord(
            check_id=check_id,
            origin="agent",
            ran_at=ran_at,
            fired=False,
            value=None,
            threshold=threshold,
            category="data_quality",
            subject=subject,
            model=model,
            detail={"lower": eff_lower, "upper": eff_upper, "total": 0},
            source_ref=source_ref,
            not_enough_data=True,
        )

    clean_values = [float(v) for v in values if not _is_null(v)]
    violations = 0
    if eff_lower is not None:
        violations += sum(1 for v in clean_values if v < eff_lower)
    if eff_upper is not None:
        violations += sum(1 for v in clean_values if v > eff_upper)

    share = round(float(violations / total), 4)
    fired = share > threshold

    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at,
        fired=fired,
        value=share,
        threshold=threshold,
        category="data_quality",
        subject=subject,
        model=model,
        detail={
            "lower": eff_lower,
            "upper": eff_upper,
            "total": total,
            "violations": violations,
            "out_of_range_share": share,
        },
        source_ref=source_ref,
        not_enough_data=False,
    )


def psi_drift(
    current: pd.DataFrame | pd.Series | Sequence[Any],
    reference: pd.DataFrame | pd.Series | Sequence[Any],
    column: str | None = None,
    *,
    ran_at: datetime,
    check_id: str = "psi_drift",
    threshold: float = PSI_DRIFT_THRESHOLD,
    num_bins: int = 10,
    model: str | None = None,
    source_ref: str = "checks/checks.jsonl",
) -> ResultRecord:
    """Calculates Population Stability Index (PSI) drift against a reference."""
    cur_values, subject = _extract_values(current, column)
    ref_values, _ = _extract_values(reference, column)

    cur_clean = [float(v) for v in cur_values if not _is_null(v)]
    ref_clean = [float(v) for v in ref_values if not _is_null(v)]

    if len(cur_clean) < 2 or len(ref_clean) < 2:
        return ResultRecord(
            check_id=check_id,
            origin="agent",
            ran_at=ran_at,
            fired=False,
            value=None,
            threshold=threshold,
            category="drift",
            subject=subject,
            model=model,
            detail={"cur_count": len(cur_clean), "ref_count": len(ref_clean)},
            source_ref=source_ref,
            not_enough_data=True,
        )

    ref_vals = np.asarray(ref_clean, dtype=float)
    cur_vals = np.asarray(cur_clean, dtype=float)

    # Bin edges based on reference quantiles
    quantiles = np.linspace(0, 1, num_bins + 1)
    raw_bins = np.quantile(ref_vals, quantiles)
    bins = np.unique(raw_bins)

    if len(bins) < 2:
        bins = np.array([float(ref_vals.min()) - 1e-5, float(ref_vals.max()) + 1e-5])

    # Extend edges to cover extreme values
    bins[0] = -np.inf
    bins[-1] = np.inf

    ref_counts, _ = np.histogram(ref_vals, bins=bins)
    cur_counts, _ = np.histogram(cur_vals, bins=bins)

    eps = 1e-4
    ref_pct = (ref_counts + eps) / np.sum(ref_counts + eps)
    cur_pct = (cur_counts + eps) / np.sum(cur_counts + eps)

    psi_val = float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))
    psi_rounded = round(psi_val, 4)
    fired = psi_rounded >= threshold

    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at,
        fired=fired,
        value=psi_rounded,
        threshold=threshold,
        category="drift",
        subject=subject,
        model=model,
        detail={"psi": psi_rounded, "bins": len(bins) - 1},
        source_ref=source_ref,
        not_enough_data=False,
    )


def pr_auc_matured(
    y_true: Sequence[int] | pd.Series,
    y_score: Sequence[float] | pd.Series,
    *,
    ran_at: datetime,
    check_id: str = "pr_auc_matured",
    threshold: float = PR_AUC_THRESHOLD,
    subject: str = "model",
    model: str | None = None,
    source_ref: str = "checks/checks.jsonl",
) -> ResultRecord:
    """Calculates PR-AUC (Average Precision) over matured labels. Fired when performance drops."""
    y_t = [int(v) for v in y_true]
    y_s = [float(v) for v in y_score]

    total_pos = sum(1 for v in y_t if v == 1)
    if total_pos == 0 or len(y_t) < 2:
        return ResultRecord(
            check_id=check_id,
            origin="agent",
            ran_at=ran_at,
            fired=False,
            value=None,
            threshold=threshold,
            category="model_performance",
            subject=subject,
            model=model,
            detail={"total": len(y_t), "positives": total_pos},
            source_ref=source_ref,
            not_enough_data=True,
        )

    # Sort descending by score
    indexed = sorted(zip(y_s, y_t, strict=True), key=lambda item: -item[0])

    tp = 0
    fp = 0
    area = 0.0

    for _, label in indexed:
        if label == 1:
            tp += 1
            precision = tp / (tp + fp)
            area += precision / total_pos
        else:
            fp += 1

    pr_auc_val = round(float(area), 4)
    fired = pr_auc_val < threshold

    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at,
        fired=fired,
        value=pr_auc_val,
        threshold=threshold,
        category="model_performance",
        subject=subject,
        model=model,
        detail={"pr_auc": pr_auc_val, "total": len(y_t), "positives": total_pos},
        source_ref=source_ref,
        not_enough_data=False,
    )


def accuracy(
    y_true: Sequence[int] | pd.Series,
    y_pred: Sequence[int | float] | pd.Series,
    *,
    ran_at: datetime,
    check_id: str = "accuracy",
    threshold: float = ACCURACY_THRESHOLD,
    subject: str = "model",
    model: str | None = None,
    source_ref: str = "checks/checks.jsonl",
) -> ResultRecord:
    """Accuracy check for team baseline. Origin is 'team'. Fired when accuracy drops below threshold."""
    y_t = [int(v) for v in y_true]
    total = len(y_t)
    if total == 0:
        return ResultRecord(
            check_id=check_id,
            origin="team",
            ran_at=ran_at,
            fired=False,
            value=None,
            threshold=threshold,
            category="model_performance",
            subject=subject,
            model=model,
            detail={"total": 0, "correct": 0},
            source_ref=source_ref,
            not_enough_data=True,
        )

    y_p: list[int] = []
    for v in y_pred:
        if isinstance(v, float) and 0.0 < v < 1.0:
            y_p.append(1 if v >= 0.5 else 0)
        else:
            y_p.append(int(v))

    correct = sum(1 for t, p in zip(y_t, y_p, strict=True) if t == p)
    acc_val = round(float(correct / total), 4)
    fired = acc_val < threshold

    return ResultRecord(
        check_id=check_id,
        origin="team",
        ran_at=ran_at,
        fired=fired,
        value=acc_val,
        threshold=threshold,
        category="model_performance",
        subject=subject,
        model=model,
        detail={"accuracy": acc_val, "total": total, "correct": correct},
        source_ref=source_ref,
        not_enough_data=False,
    )


__all__ = [
    "ACCURACY_THRESHOLD",
    "NULL_SHARE_THRESHOLD",
    "OUT_OF_RANGE_THRESHOLD",
    "PR_AUC_THRESHOLD",
    "PSI_DRIFT_THRESHOLD",
    "accuracy",
    "null_share",
    "out_of_range",
    "pr_auc_matured",
    "psi_drift",
]
