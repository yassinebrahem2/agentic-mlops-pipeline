"""Tests for tracking source creation and validation."""

from __future__ import annotations

import pytest

from mlops_agents.errors import DomainError
from tests.tracking.conftest import TrackingEnv


async def test_create_sources_round_trip(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    # 1. Create mlflow source
    mlflow_src = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="credential",
    )
    assert mlflow_src.id.startswith("trk_")
    assert mlflow_src.type == "mlflow"
    assert mlflow_src.settings == {"url": "https://mlflow.example.com"}
    assert mlflow_src.auth == "credential"
    assert mlflow_src.credential_set is False
    assert mlflow_src.last_sync is None
    assert mlflow_src.last_error is None

    # 2. Create github_actions source
    gh_src = await env.service.create_source(
        project_id=env.project_id,
        type="github_actions",
        settings={"repository": "owner/repo", "workflows": ["ci.yml"]},
        auth="none",
    )
    assert gh_src.id.startswith("trk_")
    assert gh_src.type == "github_actions"
    assert gh_src.settings == {"repository": "owner/repo", "workflows": ["ci.yml"]}
    assert gh_src.auth == "none"
    assert gh_src.credential_set is False

    # 3. List sources
    sources = await env.service.sources(env.project_id)
    assert len(sources) == 2
    assert {s.id for s in sources} == {mlflow_src.id, gh_src.id}


async def test_create_mlflow_bad_settings(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    # Userinfo in URL
    with pytest.raises(DomainError) as exc_info:
        await env.service.create_source(
            project_id=env.project_id,
            type="mlflow",
            settings={"url": "https://user:pass@mlflow.example.com"},
            auth="credential",
        )
    assert exc_info.value.status == 422
    assert "pass" not in exc_info.value.message
    assert "user:pass" not in exc_info.value.message
    if exc_info.value.detail:
        assert "pass" not in str(exc_info.value.detail)

    # file:// URL
    with pytest.raises(DomainError) as exc_info:
        await env.service.create_source(
            project_id=env.project_id,
            type="mlflow",
            settings={"url": "file:///tmp/mlflow"},
            auth="none",
        )
    assert exc_info.value.status == 422

    # Unknown key (extra key forbidden)
    with pytest.raises(DomainError) as exc_info:
        await env.service.create_source(
            project_id=env.project_id,
            type="mlflow",
            settings={"url": "https://mlflow.example.com", "unknown_key": "val"},
            auth="none",
        )
    assert exc_info.value.status == 422

    # Token in settings body refused
    with pytest.raises(DomainError) as exc_info:
        await env.service.create_source(
            project_id=env.project_id,
            type="mlflow",
            settings={"url": "https://mlflow.example.com", "token": "secret123"},
            auth="credential",
        )
    assert exc_info.value.status == 422


async def test_create_github_actions_auth_credential_refused(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    with pytest.raises(DomainError) as exc_info:
        await env.service.create_source(
            project_id=env.project_id,
            type="github_actions",
            settings={"repository": "owner/repo"},
            auth="credential",
        )
    assert exc_info.value.status == 422


async def test_create_unknown_project(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    with pytest.raises(DomainError) as exc_info:
        await env.service.create_source(
            project_id="prj_nonexistent",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="none",
        )
    assert exc_info.value.status == 404
    assert exc_info.value.code == "project_not_found"
