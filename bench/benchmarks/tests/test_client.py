from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from benchmarks.client import BenchHttpError, in_process, sign_in

type AppMaker = Callable[[str], AbstractAsyncContextManager[tuple[FastAPI, str, Path]]]


@pytest.mark.asyncio
async def test_in_process_get_projects(app_factory: AppMaker) -> None:
    async with app_factory("test-token-123") as (app, token, _):
        client = in_process(app, token)
        projects = await client.get("/projects")
        assert projects == []


@pytest.mark.asyncio
async def test_in_process_post_carries_csrf(app_factory: AppMaker, tmp_path: Path) -> None:
    async with app_factory("test-token-123") as (app, token, _):
        client = in_process(app, token)
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "source": "local",
            "path": str(repo_dir),
            "init_git": True,
        }
        project = await client.post("/projects", body)
        assert project["name"] == "test_repo"
        assert project["source"] == "local"

        # Verify that listing projects now returns this project
        projects = await client.get("/projects")
        assert len(projects) == 1
        assert projects[0]["id"] == project["id"]


@pytest.mark.asyncio
async def test_post_without_csrf_fails(app_factory: AppMaker, tmp_path: Path) -> None:
    async with app_factory("test-token-123") as (app, token, _):
        # A raw client signing in gets cookies, but if it omits X-CSRF-Token on POST, it fails with 403
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as raw_client:
            auth_res = await raw_client.post("/api/auth/token", json={"token": token})
            assert auth_res.status_code == 200

            repo_dir = tmp_path / "raw_repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            post_res = await raw_client.post(
                "/api/projects",
                json={"source": "local", "path": str(repo_dir), "init_git": True},
            )
            # Fails with 403 CSRF failed because X-CSRF-Token header was not sent
            assert post_res.status_code == 403
            assert post_res.json()["code"] == "csrf_failed"


@pytest.mark.asyncio
async def test_sign_in_missing_env_var_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MLOPS_AGENTS_ACCESS_TOKEN", raising=False)
    with pytest.raises(Exception) as exc_info:
        await sign_in("http://127.0.0.1:8765")
    message = str(exc_info.value)
    assert "MLOPS_AGENTS_ACCESS_TOKEN" in message


@pytest.mark.asyncio
async def test_http_error_contains_route_and_status_never_body(
    app_factory: AppMaker,
) -> None:
    async with app_factory("test-token-123") as (app, token, _):
        client = in_process(app, token)
        secret_content = "super-secret-password-xyz"
        with pytest.raises(BenchHttpError) as exc_info:
            # POST invalid body that will fail validation (422 Unprocessable Entity)
            await client.post("/projects", {"invalid_key": secret_content})
        error = exc_info.value
        assert error.status == 422
        assert "/api/projects" in error.route
        assert secret_content not in str(error)


@pytest.mark.asyncio
async def test_in_process_with_bad_token_fails(app_factory: AppMaker) -> None:
    async with app_factory("correct-token") as (app, _, _):
        client = in_process(app, "wrong-token")
        with pytest.raises(BenchHttpError) as exc_info:
            await client.get("/projects")
        assert exc_info.value.status == 401
