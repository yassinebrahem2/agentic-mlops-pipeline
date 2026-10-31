"""Tests for SqliteOnCallPeople and OnCallSeeder (Task 2 of Plan #472)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.notifications.constants import ON_CALL_SEEDED_KEY
from mlops_agents.notifications.contract import OnCallPerson
from mlops_agents.notifications.on_call import OnCallSeeder, SqliteOnCallPeople
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import Contact, HandoffPayload, OnCall, OnCallHours
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from tests.proposals.samples import handoff


def _sample_contact(channel: str = "email", address: str = "alice@example.com") -> Contact:
    return Contact(channel=channel, address=address)  # pyright: ignore[reportArgumentType]


def _sample_hours(start: str = "09:00", end: str = "17:00") -> OnCallHours:
    return OnCallHours(days=["mon", "tue", "wed"], start=start, end=end)  # pyright: ignore[reportArgumentType]


def _sample_on_call(
    name: str = "Alice",
    escalation_order: int = 1,
    timezone: str = "Europe/Paris",
    contacts: list[Contact] | None = None,
) -> OnCall:
    return OnCall(
        name=name,
        contacts=contacts or [_sample_contact()],
        hours=_sample_hours(),
        timezone=timezone,  # pyright: ignore[reportArgumentType]
        escalation_order=escalation_order,
    )


@pytest.fixture
async def databases(tmp_path: Any) -> AsyncGenerator[Databases, None]:
    paths = DataPaths(tmp_path)
    paths.ensure()
    dbs = Databases(paths)
    yield dbs
    await dbs.dispose()


@pytest.mark.asyncio
async def test_sqlite_on_call_people_empty_list(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    result = await people.list("prj_test")
    assert result == []


@pytest.mark.asyncio
async def test_sqlite_on_call_people_replace_and_list_ordering(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    p_charlie = _sample_on_call(name="Charlie", escalation_order=2)
    p_bob = _sample_on_call(name="Bob", escalation_order=1)
    p_alice = _sample_on_call(name="Alice", escalation_order=1)

    replaced = await people.replace("prj_test", [p_charlie, p_bob, p_alice])
    assert len(replaced) == 3
    assert [p.id for p in replaced] == ["charlie", "bob", "alice"]

    listed = await people.list("prj_test")
    # Ordered by escalation_order ASC, then id ASC:
    # escalation 1: alice, bob
    # escalation 2: charlie
    assert [p.id for p in listed] == ["alice", "bob", "charlie"]
    assert all(isinstance(p, OnCallPerson) for p in listed)
    assert listed[0].name == "Alice"
    assert listed[0].hours.start == "09:00"
    assert listed[0].timezone == "Europe/Paris"


@pytest.mark.asyncio
async def test_sqlite_on_call_people_slug_generation_and_fallback(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    p1 = _sample_on_call(name="John Doe Jr.")
    p2 = _sample_on_call(name="!@#$%^&*()")
    p3 = _sample_on_call(name="")

    stored = await people.replace("prj_test", [p1, p2, p3])
    # John Doe Jr. -> john-doe-jr
    # !@#$%^&*() -> fallback "person"
    # "" -> fallback "person-2"
    assert [p.id for p in stored] == ["john-doe-jr", "person", "person-2"]


@pytest.mark.asyncio
async def test_sqlite_on_call_people_duplicate_names_get_suffixed_ids(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    p1 = _sample_on_call(name="Alice")
    p2 = _sample_on_call(name="Bob")
    p3 = _sample_on_call(name="Alice")
    p4 = _sample_on_call(name="Alice")

    stored = await people.replace("prj_test", [p1, p2, p3, p4])
    assert [p.id for p in stored] == ["alice", "bob", "alice-2", "alice-3"]

    # Replace again to verify deterministic IDs and replacement
    stored2 = await people.replace("prj_test", [p1, p3])
    assert [p.id for p in stored2] == ["alice", "alice-2"]
    listed = await people.list("prj_test")
    assert [p.id for p in listed] == ["alice", "alice-2"]


@pytest.mark.asyncio
async def test_sqlite_on_call_people_collision_with_existing_suffix(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    # If the list literally has a person named "Alice-2" and two "Alice"s:
    p1 = _sample_on_call(name="Alice-2")
    p2 = _sample_on_call(name="Alice")
    p3 = _sample_on_call(name="Alice")

    stored = await people.replace("prj_test", [p1, p2, p3])
    ids = [p.id for p in stored]
    assert len(set(ids)) == 3
    assert ids[0] == "alice-2"
    assert ids[1] == "alice"
    assert ids[2] == "alice-3"


@pytest.mark.asyncio
async def test_sqlite_on_call_people_replace_is_atomic_and_can_clear(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    await people.replace("prj_test", [_sample_on_call(name="Alice")])
    assert len(await people.list("prj_test")) == 1

    # Replace with empty list
    await people.replace("prj_test", [])
    assert len(await people.list("prj_test")) == 0


@pytest.mark.asyncio
async def test_on_call_seeder_stored_observer(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    handoffs = SqliteHandoffs(databases)
    seeder = OnCallSeeder(people, handoffs, databases)

    payload_dict = handoff()
    payload = HandoffPayload.model_validate(payload_dict)

    view = HandoffView(
        proposal_id="prop_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )

    await seeder.stored("prj_test", view)

    # Verify seeded in people
    listed = await people.list("prj_test")
    assert len(listed) == len(payload.operations.on_call)
    assert listed[0].name == payload.operations.on_call[0].name
    assert listed[0].id == "ml-team"

    # Verify version recorded in project_settings
    engine = await databases.project("prj_test")
    async with engine.connect() as conn:
        val = await conn.scalar(
            select(project_settings.c.value).where(project_settings.c.key == ON_CALL_SEEDED_KEY)
        )
    assert val == "1"


@pytest.mark.asyncio
async def test_on_call_seeder_stored_catches_and_logs_error(
    databases: Databases, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingPeople:
        async def list(self, project_id: str) -> list[OnCallPerson]:
            return []

        async def replace(self, project_id: str, people: list[OnCall]) -> list[OnCallPerson]:
            raise RuntimeError("Database write error")

    handoffs = SqliteHandoffs(databases)
    seeder = OnCallSeeder(FailingPeople(), handoffs, databases)  # pyright: ignore[reportArgumentType]

    payload = HandoffPayload.model_validate(handoff())
    view = HandoffView(
        proposal_id="prop_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )

    with caplog.at_level(logging.ERROR):
        # Should NOT raise
        await seeder.stored("prj_test", view)

    assert "Failed to seed on-call" in caplog.text
    assert "prj_test" in caplog.text


@pytest.mark.asyncio
async def test_on_call_seeder_reconcile_seeds_unseeded_project(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    handoffs = SqliteHandoffs(databases)
    seeder = OnCallSeeder(people, handoffs, databases)

    # Project with handoff stored before seeder/observer existed
    payload = HandoffPayload.model_validate(handoff())
    await handoffs.store("prj_test", "prop_1", payload)

    # No version in project_settings yet
    engine = await databases.project("prj_test")
    async with engine.connect() as conn:
        val = await conn.scalar(
            select(project_settings.c.value).where(project_settings.c.key == ON_CALL_SEEDED_KEY)
        )
    assert val is None

    # Reconcile seeds it
    await seeder.reconcile("prj_test")

    listed = await people.list("prj_test")
    assert len(listed) == 1
    assert listed[0].id == "ml-team"

    async with engine.connect() as conn:
        val = await conn.scalar(
            select(project_settings.c.value).where(project_settings.c.key == ON_CALL_SEEDED_KEY)
        )
    assert val == "1"


@pytest.mark.asyncio
async def test_on_call_seeder_reconcile_preserves_route_edits(databases: Databases) -> None:
    people = SqliteOnCallPeople(databases)
    handoffs = SqliteHandoffs(databases)
    seeder = OnCallSeeder(people, handoffs, databases)

    payload = HandoffPayload.model_validate(handoff())
    view = HandoffView(
        proposal_id="prop_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )
    # Stored via observer
    await seeder.stored("prj_test", view)

    # User edits on-call via route
    custom_person = _sample_on_call(name="Custom Operator", escalation_order=1)
    await people.replace("prj_test", [custom_person])

    # Reconcile runs on startup
    await seeder.reconcile("prj_test")

    # The custom edit survives!
    listed = await people.list("prj_test")
    assert len(listed) == 1
    assert listed[0].name == "Custom Operator"
    assert listed[0].id == "custom-operator"


@pytest.mark.asyncio
async def test_on_call_seeder_reconcile_updates_when_handoff_version_is_newer(
    databases: Databases,
) -> None:
    people = SqliteOnCallPeople(databases)
    handoffs = SqliteHandoffs(databases)
    seeder = OnCallSeeder(people, handoffs, databases)

    # Store v1 in handoffs
    payload1 = HandoffPayload.model_validate(handoff())
    v1 = await handoffs.store("prj_test", "prop_1", payload1)
    assert v1 == 1
    view1 = await handoffs.current("prj_test")
    assert view1 is not None
    await seeder.stored("prj_test", view1)

    # Store a second handoff v2 in handoffs
    payload2_dict = handoff()
    payload2_dict["operations"]["on_call"] = [
        {
            "name": "New Team Lead",
            "contacts": [{"channel": "email", "address": "lead@acme.test"}],
            "hours": {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "08:00",
                "end": "16:00",
            },
            "timezone": "Europe/London",
            "escalation_order": 1,
        }
    ]
    payload2 = HandoffPayload.model_validate(payload2_dict)
    v2 = await handoffs.store("prj_test", "prop_2", payload2)
    assert v2 == 2

    # Reconcile runs
    await seeder.reconcile("prj_test")

    listed = await people.list("prj_test")
    assert len(listed) == 1
    assert listed[0].name == "New Team Lead"
    assert listed[0].id == "new-team-lead"

    engine = await databases.project("prj_test")
    async with engine.connect() as conn:
        val = await conn.scalar(
            select(project_settings.c.value).where(project_settings.c.key == ON_CALL_SEEDED_KEY)
        )
    assert val == "2"
