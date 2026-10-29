"""Tests for tracking credentials, storage, scrubbing and keystore loss handling."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from mlops_agents.credentials.project_secrets import tracking_ref
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import events, tracking_sources
from tests.tracking.conftest import TrackingEnv


async def test_set_credential_lifecycle(
    tracking_env: TrackingEnv, caplog: pytest.LogCaptureFixture
) -> None:
    env = tracking_env
    caplog.set_level(logging.DEBUG)

    # 1. Create source with auth="credential"
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="credential",
    )
    assert source.credential_set is False

    # 2. Put credential
    secret_value = "super-secret-token-12345"
    view = await env.service.set_credential(env.project_id, source.id, secret_value)
    assert view.credential_set is True

    # Check keystore contains it
    ref = tracking_ref(env.project_id, source.id)
    assert env.keystore.get(ref) == secret_value

    # Verify secret_value does NOT appear in settings, response, or logs
    assert secret_value not in str(view.model_dump())
    assert secret_value not in caplog.text

    # 3. Simulate keystore loss: delete secret from keystore directly
    # secret_refs row still exists in app.db, so credential_set remains True
    env.keystore.delete(ref)
    sources = await env.service.sources(env.project_id)
    assert sources[0].credential_set is True

    # 4. Syncing now should record the specific keystore missing error and not call the adapter
    adapter = env.adapters["mlflow"]
    await env.service.run_sync(env.project_id, source.id)

    assert len(adapter.calls) == 0  # No request sent!

    # Check last_error on source
    updated_sources = await env.service.sources(env.project_id)
    assert (
        updated_sources[0].last_error
        == "The credential is missing from the key store; set it again"
    )

    # Check tracking.synced event
    evts = await env.events.history(env.project_id, types=["tracking.synced"])
    assert len(evts) == 1
    assert evts[0].payload["ok"] is False
    assert evts[0].payload["error"] == "The credential is missing from the key store; set it again"


async def test_credential_scrubbed_from_adapter_error(
    tracking_env: TrackingEnv, caplog: pytest.LogCaptureFixture
) -> None:
    env = tracking_env
    caplog.set_level(logging.DEBUG)

    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="credential",
    )
    secret_token = "secret-xyz-987"
    await env.service.set_credential(env.project_id, source.id, secret_token)

    # Fake adapter returns an error leaking the secret
    env.adapters["mlflow"].result.error = f"Auth failed with token {secret_token} on remote host"

    await env.service.run_sync(env.project_id, source.id)

    updated_sources = await env.service.sources(env.project_id)
    last_err = updated_sources[0].last_error
    assert last_err is not None
    assert secret_token not in last_err
    assert "***" in last_err

    # Check events table directly in SQLite
    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        rows = list(await conn.execute(select(events.c.payload)))
        for r in rows:
            assert secret_token not in r[0]

        source_rows = list(
            await conn.execute(select(tracking_sources.c.settings, tracking_sources.c.last_error))
        )
        for s_row in source_rows:
            assert secret_token not in s_row[0]  # settings
            if s_row[1]:
                assert secret_token not in s_row[1]  # last_error

    assert secret_token not in caplog.text


async def test_set_credential_auth_none_refused(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="github_actions",
        settings={"repository": "owner/repo"},
        auth="none",
    )

    with pytest.raises(DomainError) as exc_info:
        await env.service.set_credential(env.project_id, source.id, "some-token")
    assert exc_info.value.status == 422
    assert exc_info.value.code == "tracking_no_credential_needed"


async def test_set_credential_not_found(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    with pytest.raises(DomainError) as exc_info:
        await env.service.set_credential(env.project_id, "trk_nonexistent", "token")
    assert exc_info.value.status == 404
    assert exc_info.value.code == "tracking_source_not_found"
