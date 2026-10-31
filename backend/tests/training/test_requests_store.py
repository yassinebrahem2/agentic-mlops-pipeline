"""Tests for SqliteTraining request store (Task 3, #488)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.training.contract import TrainingWindow
from mlops_agents.training.requests import SqliteTraining
from tests.providers.fake_clock import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC))


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def training_store(
    databases: Databases, events: SqliteEventLog, clock: FakeClock
) -> SqliteTraining:
    return SqliteTraining(databases=databases, events=events, clock=clock)


async def test_create_and_get_request(training_store: SqliteTraining) -> None:
    project_id = "prj_1"
    req_id = new_id("trq")
    window = TrainingWindow(
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC),
    )

    created = await training_store.create_request(
        project_id=project_id,
        request_id=req_id,
        model="churn_predictor",
        hook_id="hook-1",
        mode="train",
        status="started",
        fix_id="fix-123",
        policy_id="pol-456",
        window=window,
    )

    assert created.id == req_id
    assert created.model == "churn_predictor"
    assert created.hook_id == "hook-1"
    assert created.mode == "train"
    assert created.status == "started"
    assert created.fix_id == "fix-123"
    assert created.policy_id == "pol-456"
    assert created.window == window
    assert created.run_key is None
    assert created.ci_run_url is None

    fetched = await training_store.request(project_id, req_id)
    assert fetched is not None
    assert fetched.id == req_id
    assert fetched.model == "churn_predictor"
    assert fetched.window == window
    assert fetched.status == "started"


async def test_get_nonexistent_request_returns_none(training_store: SqliteTraining) -> None:
    res = await training_store.request("prj_1", "trq-missing")
    assert res is None


async def test_list_requests_by_model(training_store: SqliteTraining, clock: FakeClock) -> None:
    project_id = "prj_1"

    # Create 3 requests for model A and 1 for model B
    id_a1 = new_id("trq")
    clock.advance(10)
    await training_store.create_request(
        project_id=project_id,
        request_id=id_a1,
        model="model_a",
        hook_id="hook-a",
        mode="train",
    )

    id_b = new_id("trq")
    clock.advance(10)
    await training_store.create_request(
        project_id=project_id,
        request_id=id_b,
        model="model_b",
        hook_id="hook-b",
        mode="train",
    )

    id_a2 = new_id("trq")
    clock.advance(10)
    await training_store.create_request(
        project_id=project_id,
        request_id=id_a2,
        model="model_a",
        hook_id="hook-a",
        mode="evaluate",
    )

    list_a = await training_store.requests(project_id, model="model_a", limit=10)
    assert len(list_a) == 2
    # Ordered desc by started: id_a2 then id_a1
    assert list_a[0].id == id_a2
    assert list_a[1].id == id_a1

    list_b = await training_store.requests(project_id, model="model_b", limit=10)
    assert len(list_b) == 1
    assert list_b[0].id == id_b

    # Test limit
    list_a_limit1 = await training_store.requests(project_id, model="model_a", limit=1)
    assert len(list_a_limit1) == 1
    assert list_a_limit1[0].id == id_a2


async def test_set_status(training_store: SqliteTraining) -> None:
    project_id = "prj_1"
    req_id = new_id("trq")
    await training_store.create_request(
        project_id=project_id,
        request_id=req_id,
        model="model_a",
        hook_id="hook-a",
        mode="train",
        status="started",
    )

    await training_store.set_status(
        project_id=project_id,
        request_id=req_id,
        status="failed",
    )
    req = await training_store.request(project_id, req_id)
    assert req is not None
    assert req.status == "failed"

    await training_store.set_status(
        project_id=project_id,
        request_id=req_id,
        status="linked",
        run_key="run-999",
        ci_run_url="https://github.com/actions/runs/12345",
    )
    req = await training_store.request(project_id, req_id)
    assert req is not None
    assert req.status == "linked"
    assert req.run_key == "run-999"
    assert req.ci_run_url == "https://github.com/actions/runs/12345"
