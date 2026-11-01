"""The `handoff` proposal kind: preview, warnings, slots, and the parts of its idempotent apply."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.settings import get_permission_level
from mlops_agents.credentials.project_secrets import proposal_slot_ref, tracking_ref
from mlops_agents.onboarding.handoff_kind import NETWORK_WARNING
from mlops_agents.onboarding.handoff_preview import render
from mlops_agents.onboarding.kickoff import Kickoff
from mlops_agents.onboarding.kinds_common import PROJECT_KEY
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HANDOFF_KIND, CredentialSlot
from mlops_agents.proposals.payloads import Grants, HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit
from tests.agents.runtime.session_env import SessionEnv
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_mlflow import RunningFakeMlflow
from tests.onboarding.handoff_env import (
    PROJECT,
    PROPOSAL,
    UNRESOLVED_REFERENCES,
    FailingTracking,
    HandoffEnv,
    RecordingKickoff,
    build_handoff_env,
    full_payload,
    payload,
)
from tests.onboarding.kickoff_env import all_idle, handoff_messages
from tests.proposals.samples import handoff
from tests.tracking.conftest import FakeHandoffs

ALL_PARTS = ["handoff", "tracking:0", "tracking:1", "network", "level", "sync", "event", "kickoff"]


def _recorded(henv: HandoffEnv) -> RecordingKickoff:
    assert isinstance(henv.kickoff, RecordingKickoff)
    return henv.kickoff


async def _approved_events(henv: HandoffEnv) -> int:
    rows = await henv.events.page(henv.project_id, types=["handoff.approved"], limit=10)
    return len(rows)


# The kind's pure side


def test_the_kind_is_named_by_the_contract_constant(henv: HandoffEnv) -> None:
    kind = henv.kind()
    assert kind.name == HANDOFF_KIND
    assert kind.payload_model is HandoffPayload
    assert kind.key(payload("http://mlflow:5000")) == PROJECT_KEY


def test_preview_shows_every_section(henv: HandoffEnv) -> None:
    full = full_payload("http://mlflow:5000")
    preview = henv.kind().preview(full)
    body = preview.body
    assert preview.kind == "text"
    assert body == render(full)
    for expected in (
        "Model cancel",
        "Model noshow",
        "promotion: team_gate",
        "promotion: person",
        "production -> production",
        "production signal: file results/production.json",
        "placeholders window_end",
        "hold (command)",
        "cron 0 6 * * *",
        "time zone Europe/Paris",
        "every 1440 minutes",
        "mon, tue, wed, thu, fri 09:00-17:00",
        "discord #ml-alerts",
        "email ml@acme.test",
        "table hotel.bookings",
        "CI workflow nightly.yml",
        "MLflow metric recall, threshold 0.8, fires when below",
        "prepare -> train",
        "results file hotel:dq/*.json",
        "results location: file hotel:results/*.json",
        "No drift check",
        "network: off",
        "permission level: ask",
    ):
        assert expected in body, expected
    # The labels role has no time column.
    assert "time column not set" in body


def test_credentials_needed_is_one_slot_per_credential_source(henv: HandoffEnv) -> None:
    tracking: list[dict[str, Any]] = [
        {"type": "mlflow", "settings": {"url": "http://a:5000"}, "auth": "none"},
        {"type": "mlflow", "settings": {"url": "http://b:5000"}, "auth": "credential"},
    ]
    slots = henv.kind().credentials_needed(payload("http://x", tracking=tracking))
    assert len(slots) == 1
    [slot] = slots
    assert isinstance(slot, CredentialSlot)
    assert (slot.index, slot.source_type) == (1, "mlflow")
    assert "http://b:5000" in slot.label


def _grants(network: bool) -> dict[str, Any]:
    return {"network": network, "permission_level": "ask"}


@pytest.mark.parametrize(
    ("tracking", "network", "warned"),
    [
        ([{"type": "mlflow", "settings": {"url": "http://m"}, "auth": "none"}], True, True),
        ([{"type": "mlflow", "settings": {"url": "http://m"}, "auth": "none"}], False, False),
        ([{"type": "mlflow", "settings": {"url": "http://m"}, "auth": "credential"}], True, False),
        (
            [{"type": "github_actions", "settings": {"repository": "a/b"}, "auth": "none"}],
            True,
            False,
        ),
    ],
)
def test_warnings_name_an_unauthenticated_tracker_with_network_on(
    henv: HandoffEnv, tracking: list[dict[str, Any]], network: bool, warned: bool
) -> None:
    found = henv.kind().warnings(payload("http://x", tracking=tracking, grants=_grants(network)))
    assert found == ([NETWORK_WARNING] if warned else [])
    if warned:
        assert "autonomous" in NETWORK_WARNING and "bypass" in NETWORK_WARNING
        assert "audit" in NETWORK_WARNING


def test_warnings_name_each_mistyped_on_call_contact(henv: HandoffEnv) -> None:
    hours = {"days": ["mon"], "start": "09:00", "end": "17:00"}
    base = handoff()
    base["operations"]["on_call"] = [
        {
            "name": "Dana",
            "contacts": [
                {"channel": "email", "address": "dana-at-acme"},
                {"channel": "discord", "address": "u_dana"},
            ],
            "hours": hours,
            "timezone": "UTC",
            "escalation_order": 1,
        },
        {
            "name": "Eli",
            "contacts": [{"channel": "discord", "address": " "}],
            "hours": hours,
            "timezone": "UTC",
            "escalation_order": 2,
        },
    ]
    found = henv.kind().warnings(HandoffPayload.model_validate(base))
    assert len(found) == 2
    assert "Dana" in found[0] and "email" in found[0] and "dana-at-acme" not in found[0]
    assert "Eli" in found[1] and "discord" in found[1]


# Apply


async def test_full_payload_is_stored_whole(henv: HandoffEnv, mlflow: RunningFakeMlflow) -> None:
    full = full_payload(mlflow.base_url)
    result = await henv.kind().apply(PROJECT, PROPOSAL, full)

    assert "handoff" in result.applied
    current = await henv.handoffs.current(PROJECT)
    assert current is not None
    assert current.proposal_id == PROPOSAL
    assert current.payload == full


@pytest.mark.parametrize(("overrides", "reason"), UNRESOLVED_REFERENCES)
async def test_a_reference_the_project_cannot_resolve_fails_the_handoff_part(
    henv: HandoffEnv, overrides: dict[str, Any], reason: str
) -> None:
    base = payload("http://x", tracking=[]).model_dump(mode="json")
    operations = {k: v for k, v in overrides.items() if k in base["operations"]}
    top = {k: v for k, v in overrides.items() if k not in operations}
    bad = HandoffPayload.model_validate(
        {**base, **top, "operations": {**base["operations"], **operations}}
    )

    result = await henv.kind().apply(PROJECT, PROPOSAL, bad)

    assert reason in result.failed["handoff"]
    assert "handoff" not in result.applied
    assert await henv.handoffs.current(PROJECT) is None


async def test_network_unchanged_never_touches_the_sandbox(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    henv.sandbox.down = True  # set_network would raise "Docker is down"
    result = await henv.kind().apply(PROJECT, PROPOSAL, payload(mlflow.base_url))

    assert "network" in result.applied
    assert henv.sandbox.set_calls == []


async def test_network_changed_is_set_once(henv: HandoffEnv, mlflow: RunningFakeMlflow) -> None:
    grants = {"network": True, "permission_level": "ask"}
    result = await henv.kind().apply(PROJECT, PROPOSAL, payload(mlflow.base_url, grants=grants))

    assert "network" in result.applied
    assert henv.sandbox.set_calls == [True]


async def test_network_changed_with_docker_down_fails_with_its_reason(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    henv.sandbox.down = True
    grants = {"network": True, "permission_level": "ask"}
    result = await henv.kind().apply(PROJECT, PROPOSAL, payload(mlflow.base_url, grants=grants))

    assert result.failed["network"] == "Docker is down"
    assert "kickoff" in result.applied


async def test_a_tracking_source_that_fails_is_reported_and_the_rest_applies(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    failing = FailingTracking(henv.tracking, "http://refused:5000", "the tracker refused it")
    tracking: list[dict[str, Any]] = [
        {"type": "mlflow", "settings": {"url": mlflow.base_url}, "auth": "none"},
        {"type": "mlflow", "settings": {"url": "http://refused:5000"}, "auth": "none"},
    ]

    result = await henv.kind(tracking=failing).apply(
        PROJECT, PROPOSAL, payload(mlflow.base_url, tracking=tracking)
    )

    assert result.failed == {"tracking:1": "the tracker refused it"}
    assert result.applied == [p for p in ALL_PARTS if p != "tracking:1"]
    assert len(_recorded(henv).calls) == 1


async def test_a_role_naming_a_missing_connection_stores_nothing_and_starts_nothing(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    roles = [
        *payload(mlflow.base_url).model_dump(mode="json")["data_roles"],
        {"connection": "nowhere", "table": "t", "role": "other", "id_column": None,
         "time_column": None},
    ]  # fmt: skip

    result = await henv.kind().apply(PROJECT, PROPOSAL, payload(mlflow.base_url, data_roles=roles))

    assert set(result.failed) == {"handoff", "kickoff"}
    assert result.failed["kickoff"] == "not started: the handoff was not stored"
    assert result.applied == ["tracking:0", "tracking:1", "network", "level", "sync"]
    assert await henv.handoffs.current(PROJECT) is None
    assert _recorded(henv).calls == []
    assert await _approved_events(henv) == 0
    assert len(await henv.tracking.sources(PROJECT)) == 2


async def test_bypass_is_refused_and_the_level_stays(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    # The payload model forbids bypass; a hand-built object gets past it. A fake store keeps
    # the real one from re-reading an invalid payload.
    bad = payload(mlflow.base_url).model_copy(
        update={"grants": Grants.model_construct(network=False, permission_level="bypass")}
    )

    result = await henv.kind(handoffs=FakeHandoffs()).apply(PROJECT, PROPOSAL, bad)

    assert result.failed == {"level": "a proposal cannot set bypass"}
    assert await get_permission_level(henv.databases, PROJECT) == "ask"


SECRET_KEPT = "kept-Zq81xVw2-secret"
SECRET_LOST = "lost-Hn47pRt9-secret"


async def test_a_credential_moves_to_its_source_and_never_shows(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    failing = FailingTracking(henv.tracking, "http://refused:5000", "the tracker refused it")
    tracking: list[dict[str, Any]] = [
        {"type": "mlflow", "settings": {"url": mlflow.base_url}, "auth": "credential"},
        {"type": "mlflow", "settings": {"url": "http://refused:5000"}, "auth": "credential"},
    ]
    proposal = payload(mlflow.base_url, tracking=tracking)
    kept_slot = proposal_slot_ref(PROJECT, PROPOSAL, 0)
    lost_slot = proposal_slot_ref(PROJECT, PROPOSAL, 1)
    await henv.secrets.put(kept_slot, SECRET_KEPT)
    await henv.secrets.put(lost_slot, SECRET_LOST)
    kind = henv.kind(tracking=failing)

    result = await kind.apply(PROJECT, PROPOSAL, proposal)

    [source] = await henv.tracking.sources(PROJECT)
    assert source.credential_set
    assert henv.keystore.get(tracking_ref(PROJECT, source.id)) == SECRET_KEPT
    for slot in (kept_slot, lost_slot):
        assert henv.keystore.get(slot) is None
        assert not await henv.secrets.is_set(slot)
    events = await henv.events.page(PROJECT, limit=500)
    engine = await henv.databases.project(PROJECT)
    async with engine.connect() as conn:
        audit_rows = (await conn.execute(select(audit))).all()
    shown = "\n".join(
        [
            kind.preview(proposal).model_dump_json(),
            result.model_dump_json(),
            *(e.model_dump_json() for e in events),
            *(str(tuple(row)) for row in audit_rows),
        ]
    )
    for secret in (SECRET_KEPT, SECRET_LOST):
        for piece in (secret, secret[:8], secret[-8:]):
            assert piece not in shown


async def test_a_slot_never_filled_leaves_the_source_without_a_credential(
    henv: HandoffEnv, mlflow: RunningFakeMlflow
) -> None:
    tracking = [{"type": "mlflow", "settings": {"url": mlflow.base_url}, "auth": "credential"}]

    result = await henv.kind().apply(PROJECT, PROPOSAL, payload(mlflow.base_url, tracking=tracking))

    assert "tracking:0" in result.applied
    [source] = await henv.tracking.sources(PROJECT)
    assert not source.credential_set


# With the real kickoff


@pytest.fixture
async def live(
    env: SessionEnv,
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
) -> AsyncIterator[HandoffEnv]:
    built = await build_handoff_env(
        tmp_path=tmp_path,
        paths=paths,
        databases=databases,
        keystore=keystore,
        project_id=env.project_id,
        kickoff=RecordingKickoff(),
    )
    built.kickoff = Kickoff(databases, env.manager, built.handoffs)
    yield built
    await all_idle(env.manager, env.project_id)
    await built.close()


async def test_every_part_applies(
    live: HandoffEnv, env: SessionEnv, mlflow: RunningFakeMlflow
) -> None:
    grants = {"network": True, "permission_level": "autonomous"}
    approved = payload(mlflow.base_url, grants=grants)

    result = await live.kind().apply(live.project_id, PROPOSAL, approved)
    await all_idle(env.manager, live.project_id)

    assert result.applied == ALL_PARTS
    assert result.failed == {}
    current = await live.handoffs.current(live.project_id)
    assert current is not None and current.payload == approved
    assert len(await live.tracking.sources(live.project_id)) == 2
    assert live.sandbox.network is True
    assert await get_permission_level(live.databases, live.project_id) == "autonomous"
    [event] = await live.events.page(live.project_id, types=["handoff.approved"], limit=10)
    assert (event.source, event.subject_id, event.payload) == (
        "onboarding",
        PROPOSAL,
        {"proposal_id": PROPOSAL},
    )
    assert len(await handoff_messages(live.databases, live.project_id)) == 4


async def test_apply_twice_is_idempotent(
    live: HandoffEnv, env: SessionEnv, mlflow: RunningFakeMlflow
) -> None:
    approved = payload(
        mlflow.base_url,
        tracking=[{"type": "mlflow", "settings": {"url": mlflow.base_url}, "auth": "none"}],
    )
    kind = live.kind()

    first = await kind.apply(live.project_id, PROPOSAL, approved)
    await all_idle(env.manager, live.project_id)
    second = await kind.apply(live.project_id, PROPOSAL, approved)
    await all_idle(env.manager, live.project_id)

    assert first.failed == second.failed == {}
    current = await live.handoffs.current(live.project_id)
    assert current is not None and current.version == 1
    assert len(await live.tracking.sources(live.project_id)) == 1
    assert await _approved_events(live) == 1
    assert len(await handoff_messages(live.databases, live.project_id)) == 4
