"""Tracking adapter registry."""

from __future__ import annotations

import httpx2

from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.tracking.contract import SourceType, TrackingAdapter


def default_adapters(
    http: httpx2.AsyncClient, github: GitHubAuth
) -> dict[SourceType, TrackingAdapter]:
    """Default tracking adapters. Empty for now (tasks 2 and 3 add MLflow and GitHub Actions)."""
    return {}
