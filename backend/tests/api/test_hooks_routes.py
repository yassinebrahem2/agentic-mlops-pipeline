"""Tests for /api/projects/{project_id}/hooks routes."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.credentials.project_secrets import hook_ref
from mlops_agents.proposals.payloads import CommandSpec
from tests.api.conftest import add_local_project


@pytest.mark.asyncio
async def test_hooks_routes_full_lifecycle(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    # 1. Register a hook
    spec = CommandSpec(
        kind="train",
        type="command",
        template="python train.py",
        workflow=None,
        inputs={},
        placeholders=[],
        deploys=False,
        entry_files=[],
    )
    hook = await services.action_hooks.register(
        project_id,
        spec=spec,
        name="test-hook",
        model="iris",
        source="proposal",
    )
    hook_id = hook.id
    assert hook.credential_set is False
    assert hook.status == "active"

    # 2. GET /api/projects/{project_id}/hooks
    resp = await client.get(f"/api/projects/{project_id}/hooks")
    assert resp.status_code == 200
    hooks_list = resp.json()
    assert len(hooks_list) >= 1
    target_hook = next(h for h in hooks_list if h["id"] == hook_id)
    assert target_hook["name"] == "test-hook"
    assert target_hook["credential_set"] is False

    # 3. PUT /api/projects/{project_id}/hooks/{hook_id}/credential
    secret_val = "hook-super-secret-token-777"
    with caplog.at_level(logging.DEBUG):
        put_cred_resp = await client.put(
            f"/api/projects/{project_id}/hooks/{hook_id}/credential",
            json={"credential": secret_val},
        )
    assert put_cred_resp.status_code == 200
    cred_hook = put_cred_resp.json()
    assert cred_hook["id"] == hook_id
    assert cred_hook["credential_set"] is True
    # Verify value is absent from response body and logs
    assert secret_val not in put_cred_resp.text
    assert secret_val not in caplog.text

    # Verify value is absent from event history
    events = await services.events.history(
        project_id,
        types=["hook.suspended", "hook.ran", "hook.failed"],
    )
    for evt in events:
        assert secret_val not in str(evt.payload)

    # Keystore actually holds the secret
    ref = hook_ref(project_id, hook_id)
    assert services.keystore.get(ref) == secret_val

    # 4. PUT /api/projects/{project_id}/hooks/{hook_id}/enabled
    # Disable
    dis_resp = await client.put(
        f"/api/projects/{project_id}/hooks/{hook_id}/enabled",
        json={"enabled": False},
    )
    assert dis_resp.status_code == 200
    assert dis_resp.json()["status"] == "disabled"

    # Enable
    en_resp = await client.put(
        f"/api/projects/{project_id}/hooks/{hook_id}/enabled",
        json={"enabled": True},
    )
    assert en_resp.status_code == 200
    assert en_resp.json()["status"] == "active"

    # 5. DELETE /api/projects/{project_id}/hooks/{hook_id}/credential
    del_cred_resp = await client.delete(f"/api/projects/{project_id}/hooks/{hook_id}/credential")
    assert del_cred_resp.status_code == 200
    assert del_cred_resp.json()["credential_set"] is False
    assert services.keystore.get(ref) is None
    assert not await services.project_secrets.is_set(ref)

    # 6. Removing project deletes the ref
    # Set credential again first
    await client.put(
        f"/api/projects/{project_id}/hooks/{hook_id}/credential",
        json={"credential": secret_val},
    )
    assert services.keystore.get(ref) == secret_val
    assert await services.project_secrets.is_set(ref)

    # Remove project
    del_proj = await client.delete(f"/api/projects/{project_id}")
    assert del_proj.status_code == 204
    assert services.keystore.get(ref) is None
    assert not await services.project_secrets.is_set(ref)


@pytest.mark.asyncio
async def test_hooks_routes_unknown_hook_404(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Unknown hook on PUT credential
    resp1 = await client.put(
        f"/api/projects/{project_id}/hooks/hok_nonexistent/credential",
        json={"credential": "xyz"},
    )
    assert resp1.status_code == 404

    # Unknown hook on DELETE credential
    resp2 = await client.delete(f"/api/projects/{project_id}/hooks/hok_nonexistent/credential")
    assert resp2.status_code == 404

    # Unknown hook on PUT enabled
    resp3 = await client.put(
        f"/api/projects/{project_id}/hooks/hok_nonexistent/enabled",
        json={"enabled": False},
    )
    assert resp3.status_code == 404
