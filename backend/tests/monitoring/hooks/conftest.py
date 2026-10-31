"""Fixtures and test doubles for monitoring hooks tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import pytest

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookRefused,
    HookResult,
    HookView,
    PinnedFile,
)
from mlops_agents.monitoring.hooks.handoff import HandoffHooks
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.storage.databases import Databases
from tests.conftest import memory_project_secrets
from tests.providers.fake_clock import FakeClock


class FakeRunner:
    def __init__(
        self,
        runner_type: Literal["command", "ci_dispatch"] = "command",
        result: HookResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.type: Literal["command", "ci_dispatch"] = runner_type
        self.result = result or HookResult(ok=True, detail="executed successfully")
        self.exc = exc
        self.calls: list[tuple[str, HookView, Mapping[str, str], Authority]] = []

    async def run(
        self,
        project_id: str,
        hook: HookView,
        params: Mapping[str, str],
        authority: Authority,
    ) -> HookResult:
        self.calls.append((project_id, hook, params, authority))
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeLifecycle:
    def __init__(
        self,
        name: str = "lifecycle",
        refusal: str | None = None,
    ) -> None:
        self.name = name
        self.refusal = refusal
        self.order: list[str] = []
        self.before_calls: list[tuple[str, HookView, HookCall]] = []
        self.after_calls: list[tuple[str, HookView, HookCall, HookResult]] = []

    async def before(self, project_id: str, hook: HookView, call: HookCall) -> HookCall:
        self.order.append(f"{self.name}:before")
        self.before_calls.append((project_id, hook, call))
        if self.refusal is not None:
            raise HookRefused(self.refusal)
        return call

    async def after(
        self,
        project_id: str,
        hook: HookView,
        call: HookCall,
        result: HookResult,
    ) -> None:
        self.order.append(f"{self.name}:after")
        self.after_calls.append((project_id, hook, call, result))


class FakePinner:
    def __init__(
        self,
        commit: str = "abc1234",
        pins: list[PinnedFile] | None = None,
    ) -> None:
        self.commit = commit
        self.pins_list = pins if pins is not None else [PinnedFile(path="train.py", sha256="hash1")]
        self.calls: list[tuple[str, HookView]] = []

    async def pins(self, project_id: str, hook: HookView) -> tuple[str, list[PinnedFile]]:
        self.calls.append((project_id, hook))
        return self.commit, self.pins_list


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def secrets(databases: Databases) -> ProjectSecrets:
    return memory_project_secrets(databases)


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def handoffs(databases: Databases) -> SqliteHandoffs:
    return SqliteHandoffs(databases)


@pytest.fixture
def service(
    databases: Databases,
    secrets: ProjectSecrets,
    events: SqliteEventLog,
    clock: FakeClock,
) -> HooksService:
    return HooksService(databases=databases, secrets=secrets, events=events, clock=clock)


@pytest.fixture
def handoff_hooks(service: HooksService, handoffs: SqliteHandoffs) -> HandoffHooks:
    return HandoffHooks(hooks=service, handoffs=handoffs)
