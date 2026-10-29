"""Tracking adapter registry."""

from __future__ import annotations

import httpx2

from mlops_agents.tracking.adapters.github_actions import GithubActionsAdapter
from mlops_agents.tracking.adapters.mlflow import MlflowAdapter
from mlops_agents.tracking.contract import SourceType, TrackingAdapter


def default_adapters(
    http: httpx2.AsyncClient,
) -> dict[SourceType, TrackingAdapter]:
    """Default tracking adapters."""
    return {
        "mlflow": MlflowAdapter(http=http),
        "github_actions": GithubActionsAdapter(http=http),
    }
