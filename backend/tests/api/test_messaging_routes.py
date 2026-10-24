from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI

from tests.api.conftest import add_local_project


async def test_anonymous_cannot_access_messaging_settings(anonymous: httpx2.AsyncClient) -> None:
    url = "/api/projects/prj_dummy/settings/messaging"
    assert (await anonymous.get(url)).status_code == 401
    assert (await anonymous.put(url, json={"max_hops": 3, "max_tokens": 1000})).status_code == 401


async def test_messaging_settings_default_and_round_trip(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    url = f"/api/projects/{project_id}/settings/messaging"

    assert (await client.get(url)).json() == {"max_hops": 6, "max_tokens": 300000}

    put = await client.put(url, json={"max_hops": 3, "max_tokens": 12000})
    assert put.status_code == 200, put.text
    assert put.json() == {"max_hops": 3, "max_tokens": 12000}
    assert (await client.get(url)).json() == {"max_hops": 3, "max_tokens": 12000}


async def test_messaging_settings_reject_values_below_one(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    url = f"/api/projects/{project_id}/settings/messaging"

    for body in ({"max_hops": 0, "max_tokens": 10}, {"max_hops": 2, "max_tokens": 0}):
        assert (await client.put(url, json=body)).status_code == 422
    assert (await client.get(url)).json() == {"max_hops": 6, "max_tokens": 300000}


async def test_messaging_settings_of_an_unknown_project_are_not_found(
    client: httpx2.AsyncClient,
) -> None:
    res = await client.get("/api/projects/prj_missing/settings/messaging")

    assert res.status_code == 404
