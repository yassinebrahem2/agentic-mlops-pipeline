"""Onboarding end to end, in process, through the HTTP API: the scripted Onboarding proposes the
connection and the handoff, both are approved, the audits start; a restart neither repeats nor
loses a kickoff, and a second handoff starts its own."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.hooks.names import DATA_SUMMARY_HOOK
from mlops_agents.api.services import Services
from mlops_agents.onboarding.briefs import KICKOFF_AGENTS
from mlops_agents.onboarding.kickoff import HANDOFF_AUTHOR
from mlops_agents.proposals.contract import CONNECTIONS_KIND, HANDOFF_KIND
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.fakes.fake_mlflow import FakeMlflowState, RunningFakeMlflow, start_fake_mlflow
from tests.fakes.fake_openai import RunningFake
from tests.fakes.onboarding_script import (
    ASK_FOR_DATA,
    AUDIT_CONTEXT,
    AUDIT_NO_DATA,
    BOOKINGS,
    CONNECTION_NAME,
    DATA_PATH_KEY,
    MLFLOW_URL_KEY,
    MODEL_NAME,
    ONBOARDING_COMPLETE,
    audit_reply,
    write_data_folder,
)
from tests.onboarding.kickoff_env import all_idle, handoff_messages

ONBOARDING = "onboarding"
ONBOARDING_MODEL = "fake-onboarding"
AUDIT_MODEL = "fake-audit"
# Only the data_summary hook writes a table's header line.
TABLE_LINE = f'Table "{BOOKINGS}"'
WAIT_S = 30


@pytest.fixture
def mlflow() -> Iterator[RunningFakeMlflow]:
    state = FakeMlflowState()
    state.add_experiment("hotel-cancel")
    running = start_fake_mlflow(state)
    yield running
    running.stop()


async def _use_fake_models(services: Services, fake: RunningFake) -> None:
    provider = await services.providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    for wire_id in (ONBOARDING_MODEL, AUDIT_MODEL):
        await services.providers.add_model(
            ModelCreate(provider=provider.name, wire_model_id=wire_id)
        )
        await services.providers.verify_model(f"fake:{wire_id}")
    await services.agent_settings.set_default_model(ONBOARDING, f"fake:{ONBOARDING_MODEL}")
    for agent in KICKOFF_AGENTS:
        await services.agent_settings.set_default_model(agent, f"fake:{AUDIT_MODEL}")


def _text(message: dict[str, Any]) -> str:
    return "".join(p["text"] for p in message["parts"] if p["type"] == "text")


async def _session(http: httpx2.AsyncClient, project_id: str, agent: str) -> dict[str, Any]:
    response = await http.get(f"/api/projects/{project_id}/agents/{agent}/session")
    assert response.status_code == 200, response.text
    return response.json()


async def _send(http: httpx2.AsyncClient, project_id: str, text: str) -> None:
    response = await http.post(
        f"/api/projects/{project_id}/agents/{ONBOARDING}/session/messages", json={"text": text}
    )
    assert response.status_code == 201, response.text


async def _replies(http: httpx2.AsyncClient, project_id: str, text: str, count: int) -> None:
    """Until Onboarding has answered `text` `count` times and its turn has ended."""
    async with asyncio.timeout(WAIT_S):
        while True:
            session = await _session(http, project_id, ONBOARDING)
            said = [
                m for m in session["messages"] if m["author_kind"] == "agent" and _text(m) == text
            ]
            if len(said) >= count and not session["busy"]:
                return
            await asyncio.sleep(0.05)


async def _decide(
    http: httpx2.AsyncClient, project_id: str, proposal_kind: str, *, approve: bool
) -> None:
    """Decides the one pending approval once its turn waits: the Inbox list holds only those."""
    async with asyncio.timeout(WAIT_S):
        while True:
            response = await http.get(f"/api/projects/{project_id}/approvals")
            pending: list[dict[str, Any]] = response.json()
            if pending:
                break
            await asyncio.sleep(0.05)
    [approval] = pending
    assert approval["proposal_kind"] == proposal_kind
    response = await http.post(
        f"/api/projects/{project_id}/approvals/{approval['id']}/decision",
        json={"approve": approve},
    )
    assert response.status_code == 200, response.text


async def _onboard(
    http: httpx2.AsyncClient, project_id: str, folder: Path, mlflow_url: str, *, approve: bool
) -> None:
    await _send(http, project_id, f"{DATA_PATH_KEY}{folder}\n{MLFLOW_URL_KEY}{mlflow_url}")
    await _decide(http, project_id, CONNECTIONS_KIND, approve=approve)
    await _decide(http, project_id, HANDOFF_KIND, approve=approve)


async def _get(http: httpx2.AsyncClient, path: str) -> Any:
    response = await http.get(path)
    assert response.status_code == 200, response.text
    return response.json()


async def _synced_source(http: httpx2.AsyncClient, project_id: str) -> dict[str, Any]:
    async with asyncio.timeout(WAIT_S):
        while True:
            sources = await _get(http, f"/api/projects/{project_id}/tracking/sources")
            if sources and sources[0]["last_sync"] is not None:
                [source] = sources
                return source
            await asyncio.sleep(0.05)


def _onboarding_requests(fake: RunningFake) -> dict[int, list[dict[str, Any]]]:
    """Onboarding's requests after its data message, by the tool results they carry."""
    found: dict[int, list[dict[str, Any]]] = {}
    for body in fake.state.requests:
        if body.get("model") != ONBOARDING_MODEL or not body.get("tools"):
            continue
        messages: list[dict[str, Any]] = body["messages"]
        asked = [
            i
            for i, m in enumerate(messages)
            if m["role"] == "user" and DATA_PATH_KEY in str(m.get("content"))
        ]
        if asked:
            results = sum(1 for m in messages[asked[-1] :] if m["role"] == "tool")
            found.setdefault(results, messages)
    return found


async def _audits(
    http: httpx2.AsyncClient, services: Services, project_id: str, rounds: int
) -> None:
    """Each audit agent got one kickoff per approved handoff and answered each before the next
    one came, the last kickoff being the current handoff's; the data summary's table line
    reached the turns of exactly the agents whose definitions name the hook (the others found
    the table only in the kickoff message)."""
    await all_idle(services.sessions, project_id)
    current = await _get(http, f"/api/projects/{project_id}/handoff")
    marker = f"[handoff {current['proposal_id']}]"
    for agent in KICKOFF_AGENTS:
        messages = (await _session(http, project_id, agent))["messages"]
        story = [
            m for m in messages if m["author_name"] == HANDOFF_AUTHOR or m["author_kind"] == "agent"
        ]
        kinds = ["kickoff" if m["author_name"] == HANDOFF_AUTHOR else "reply" for m in story]
        assert kinds == ["kickoff", "reply"] * rounds, (agent, kinds)
        assert _text(story[-2]).startswith(marker), (agent, _text(story[-2])[:80])
        has_hook = DATA_SUMMARY_HOOK in services.catalog.get(agent).hook_names
        for reply in (_text(m) for m in story[1::2]):
            assert reply.startswith(AUDIT_CONTEXT), (agent, reply)
            assert reply.startswith(AUDIT_CONTEXT + TABLE_LINE) == has_hook, (agent, reply)


async def test_the_onboarding_story_survives_a_restart_and_a_second_handoff_kicks_off_again(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
    mlflow: RunningFakeMlflow,
) -> None:
    folder = tmp_path / "exports"
    write_data_folder(folder)
    home = tmp_path / "data"

    async with started_app(home) as app, signed_in(app, LOCAL_URL) as http:
        services: Services = app.state.services
        await _use_fake_models(services, fake_openai)
        project_id = await add_local_project(app, tmp_path, git_env)

        await _send(http, project_id, 'Please onboard the project "test-repo".')
        await _replies(http, project_id, ASK_FOR_DATA, 1)
        await _onboard(http, project_id, folder, mlflow.base_url, approve=True)
        await _replies(http, project_id, ONBOARDING_COMPLETE, 1)

        [connection] = await _get(http, f"/api/projects/{project_id}/connections")
        assert (connection["name"], connection["status"]) == (CONNECTION_NAME, "ok")
        source = await _synced_source(http, project_id)
        assert (source["type"], source["settings"]) == ("mlflow", {"url": mlflow.base_url})
        assert source["last_error"] is None
        handoff = await _get(http, f"/api/projects/{project_id}/handoff")
        assert handoff["payload"]["models"][0]["name"] == MODEL_NAME

        # The waiting turn resumed with its hooks rendered again: the new connection's summary
        # is in the request after the approval and was not in the one before it.
        requests = _onboarding_requests(fake_openai)
        assert audit_reply(requests[0]) == AUDIT_NO_DATA
        assert audit_reply(requests[1]).startswith(AUDIT_CONTEXT + TABLE_LINE)

        await _audits(http, services, project_id, rounds=1)
        onboarding = (await _session(http, project_id, ONBOARDING))["messages"]
        assert all(m["author_name"] != HANDOFF_AUTHOR for m in onboarding)
        first = await handoff_messages(services.databases, project_id)
        assert sorted(m.session_agent for m in first) == sorted(KICKOFF_AGENTS)

    async with started_app(home) as app, signed_in(app, LOCAL_URL) as http:
        services = app.state.services
        await all_idle(services.sessions, project_id)
        assert await handoff_messages(services.databases, project_id) == first

        await _onboard(http, project_id, folder, mlflow.base_url, approve=True)
        await _replies(http, project_id, ONBOARDING_COMPLETE, 2)
        await _audits(http, services, project_id, rounds=2)
        found = await handoff_messages(services.databases, project_id)

    assert len(found) == 8
    assert found[:4] == first
    assert sorted(m.session_agent for m in found[4:]) == sorted(KICKOFF_AGENTS)


async def test_rejecting_the_proposals_creates_nothing(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
    mlflow: RunningFakeMlflow,
) -> None:
    folder = tmp_path / "exports"
    write_data_folder(folder)

    async with started_app(tmp_path / "data") as app, signed_in(app, LOCAL_URL) as http:
        services: Services = app.state.services
        await _use_fake_models(services, fake_openai)
        project_id = await add_local_project(app, tmp_path, git_env)

        # A rejected connection is still a step: the fake goes on to the handoff, rejected too.
        await _onboard(http, project_id, folder, mlflow.base_url, approve=False)
        await _replies(http, project_id, ONBOARDING_COMPLETE, 1)

        assert await _get(http, f"/api/projects/{project_id}/connections") == []
        assert await _get(http, f"/api/projects/{project_id}/tracking/sources") == []
        assert await _get(http, f"/api/projects/{project_id}/handoff") is None
        assert await handoff_messages(services.databases, project_id) == []
