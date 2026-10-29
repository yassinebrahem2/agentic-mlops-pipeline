"""Promotion constants."""

from __future__ import annotations

from typing import Final

REQUIRED_REPORT_FIELDS: Final[tuple[str, ...]] = (
    "fix_chosen",
    "data_window",
    "leakage",
    "label_maturity",
    "imbalance",
    "training_serving_skew",
    "risks_and_watch",
    "rollback_plan",
)
