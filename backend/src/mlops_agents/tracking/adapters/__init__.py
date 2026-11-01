"""Tracking adapter registry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx2

from mlops_agents.tracking.adapters.github_actions import GithubActionsAdapter
from mlops_agents.tracking.adapters.mlflow import MlflowAdapter
from mlops_agents.tracking.contract import SourceType, TrackingAdapter


def default_adapters(
    http: httpx2.AsyncClient,
    clock: Callable[[], datetime],
) -> dict[SourceType, TrackingAdapter]:
    """Default tracking adapters, reading the time from `clock`."""
    return {
        "mlflow": MlflowAdapter(http=http, clock=clock),
        "github_actions": GithubActionsAdapter(http=http, clock=clock),
    }
