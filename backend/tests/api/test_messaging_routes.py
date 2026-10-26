from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx2
from fastapi import FastAPI

from mlops_agents.agents.tools.base import FileAccess, ToolContext
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


async def test_the_app_wires_send_message_to_a_bound_bus_with_the_catalog(app: FastAPI) -> None:
    tool = app.state.services.tools.get("SendMessage")
    assert tool is not None
    ctx = ToolContext(
        project_id="prj_any",
        agent="orchestrator",
        session_id="ses_any",
        turn_id="trn_any",
        files=cast(FileAccess, object()),
        task=None,
    )

    out = await tool.invoke(ctx, {"to": "orchestrator", "message": "x"})

    # An unbound bus or one without the catalog raises; a wired one refuses by can_message.
    assert out.is_error
    assert "may not message" in out.content


async def test_the_app_wires_list_agents_bound_with_catalog_and_status_board(app: FastAPI) -> None:
    tool = app.state.services.tools.get("ListAgents")
    assert tool is not None
    ctx = ToolContext(
        project_id="prj_any",
        agent="orchestrator",
        session_id="ses_any",
        turn_id="trn_any",
        files=cast(FileAccess, object()),
        task=None,
    )

    out = await tool.invoke(ctx, {})
    assert not out.is_error
    assert "Name: orchestrator" in out.content
