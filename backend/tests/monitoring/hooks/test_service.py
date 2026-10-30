"""Tests for HooksService: registration, pins, approval, suspension, runs, lifecycles, and refusals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import pytest

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.constants import HOOK_DETAIL_MAX_CHARS
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookRefused,
    HookResult,
    HookView,
    PinnedFile,
)
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.payloads import CommandSpec
from tests.monitoring.hooks.conftest import FakeLifecycle, FakePinner, FakeRunner
from tests.proposals.samples import command


def _sample_spec(kind: str = "train", cmd_type: str = "command") -> CommandSpec:
    return CommandSpec.model_validate(
        command(
            kind=kind,
            type=cmd_type,
            template="python train.py --model {model}",
            placeholders=["model"],
        )
    )


@pytest.mark.asyncio
async def test_run_order_before_runner_after(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    order: list[str] = []

    class TrackedLifecycle:
        def __init__(self, name: str) -> None:
            self.name = name

        async def before(self, project_id: str, hook: HookView, call: HookCall) -> HookCall:
            order.append(f"{self.name}:before")
            return call

        async def after(
            self, project_id: str, hook: HookView, call: HookCall, result: HookResult
        ) -> None:
            order.append(f"{self.name}:after")

    class TrackedRunner:
        type: Literal["command", "ci_dispatch"] = "command"

        async def run(
            self,
            project_id: str,
            hook: HookView,
            params: Mapping[str, str],
            authority: Authority,
        ) -> HookResult:
            order.append("runner")
            return HookResult(ok=True, detail="success")

    lc1 = TrackedLifecycle("lc1")
    lc2 = TrackedLifecycle("lc2")
    service.add_lifecycle(lc1)
    service.add_lifecycle(lc2)
    service.add_runner(TrackedRunner())

    call = HookCall(hook_id=hook.id, reason="retrain model")
    auth = Authority(basis="rule", ref="rul_1", reason="metric drop", evidence=["ev1"])

    result = await service.run(project_id, call, auth)
    assert result.ok is True
    assert order == ["lc1:before", "lc2:before", "runner", "lc1:after", "lc2:after"]


@pytest.mark.asyncio
async def test_lifecycle_before_raising_refused_skips_runner_and_records_incident(
    service: HooksService,
    events: Any,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    runner = FakeRunner(runner_type="command")
    service.add_runner(runner)

    lc = FakeLifecycle(
        name="blocking", refusal="Safety check failed: https://user:pass@secret.com/info"
    )
    service.add_lifecycle(lc)

    call = HookCall(hook_id=hook.id, incident_id="inc_42", reason="retrain model")
    auth = Authority(basis="policy", ref="pol_1", reason="high drift", evidence=[])

    with pytest.raises(HookRefused, match="Safety check failed"):
        await service.run(project_id, call, auth)

    # Runner was skipped
    assert len(runner.calls) == 0

    # Event recorded on the incident
    logged = await events.history(project_id, types=["hook.failed"])
    assert len(logged) == 1
    event = logged[0]
    assert event.subject_id == "inc_42"
    assert event.payload["hook_id"] == hook.id
    assert event.payload["kind"] == "train"
    assert event.payload["basis"] == "policy"

    # Hook runs row recorded
    runs = await service.runs(project_id)
    assert len(runs) == 1
    run = runs[0]
    assert run.ok is False
    assert run.incident_id == "inc_42"
    assert run.basis == "policy"
    # Detail is scrubbed
    assert run.detail is not None
    assert "user:pass" not in run.detail
    assert "***@" in run.detail


@pytest.mark.asyncio
async def test_failing_runner_is_called_exactly_once_and_detail_scrubbed(
    service: HooksService,
    events: Any,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    runner = FakeRunner(
        runner_type="command",
        exc=RuntimeError("Process failed with token: https://user:secret123@api.com/v1"),
    )
    service.add_runner(runner)

    call = HookCall(hook_id=hook.id, incident_id="inc_99", reason="retrain model")
    auth = Authority(basis="approval", ref="apr_1", reason="user approved", evidence=["user_ok"])

    result = await service.run(project_id, call, auth)
    assert result.ok is False
    # Runner was called exactly once - no retry
    assert len(runner.calls) == 1

    # Detail is scrubbed
    assert "secret123" not in result.detail
    assert "***@" in result.detail

    # hook.failed recorded on incident
    logged = await events.history(project_id, types=["hook.failed"])
    assert len(logged) == 1
    assert logged[0].subject_id == "inc_99"
    assert logged[0].payload["kind"] == "train"
    assert logged[0].payload["basis"] == "approval"

    # hook_runs row has incident, kind, basis
    runs = await service.runs(project_id)
    assert len(runs) == 1
    assert runs[0].incident_id == "inc_99"
    assert runs[0].kind == "train"
    assert runs[0].basis == "approval"
    assert runs[0].ok is False


@pytest.mark.asyncio
async def test_disabled_hook_and_missing_runner_are_refused(
    service: HooksService,
    events: Any,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    # 1. Disabled hook
    await service.set_enabled(project_id, hook.id, False)

    call = HookCall(hook_id=hook.id, reason="run disabled")
    auth = Authority(basis="none", ref=None, reason="manual", evidence=[])

    with pytest.raises(HookRefused, match="disabled"):
        await service.run(project_id, call, auth)

    # Recorded failure on hook (since no incident)
    logged = await events.history(project_id, types=["hook.failed"])
    assert len(logged) == 1
    assert logged[0].subject_id == hook.id

    # 2. Missing runner for type
    # Re-enable hook, but do not register runner for "command"
    await service.set_enabled(project_id, hook.id, True)

    with pytest.raises(HookRefused, match="No runner registered for type 'command'"):
        await service.run(project_id, call, auth)

    logged2 = await events.history(project_id, types=["hook.failed"])
    assert len(logged2) == 2


@pytest.mark.asyncio
async def test_suspended_hook_is_not_refused_in_run(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    # Suspend hook
    await service.suspend(project_id, hook.id, "Drift detected")

    runner = FakeRunner(runner_type="command")
    service.add_runner(runner)

    call = HookCall(hook_id=hook.id, reason="run while suspended")
    auth = Authority(basis="none", ref=None, reason="manual", evidence=[])

    result = await service.run(project_id, call, auth)
    assert result.ok is True
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_detail_capped_at_max_chars(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    huge_detail = "X" * 5000
    runner = FakeRunner(runner_type="command", result=HookResult(ok=True, detail=huge_detail))
    service.add_runner(runner)

    call = HookCall(hook_id=hook.id, reason="test capping")
    auth = Authority(basis="none", ref=None, reason="test", evidence=[])

    result = await service.run(project_id, call, auth)
    assert len(result.detail) == HOOK_DETAIL_MAX_CHARS

    runs = await service.runs(project_id)
    assert len(runs) == 1
    assert runs[0].detail is not None
    assert len(runs[0].detail) == HOOK_DETAIL_MAX_CHARS


@pytest.mark.asyncio
async def test_pinner_stored_on_register_and_replaced_on_approve(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()

    pinner = FakePinner(commit="commit_1", pins=[PinnedFile(path="train.py", sha256="sha_1")])
    service.set_pinner(pinner)

    # Register calls pinner
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")
    assert hook.approved_commit == "commit_1"
    assert len(hook.pins) == 1
    assert hook.pins[0].path == "train.py"
    assert hook.pins[0].sha256 == "sha_1"

    # Change pinner output and approve
    pinner.commit = "commit_2"
    pinner.pins_list = [
        PinnedFile(path="train.py", sha256="sha_2"),
        PinnedFile(path="eval.py", sha256="sha_eval"),
    ]

    approved = await service.approve(project_id, hook.id, by="reviewer")
    assert approved.approved_commit == "commit_2"
    assert len(approved.pins) == 2
    paths = {p.path for p in approved.pins}
    assert paths == {"train.py", "eval.py"}


@pytest.mark.asyncio
async def test_suspend_then_set_enabled_true_leaves_suspended_and_approve_clears(
    service: HooksService,
    events: Any,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")

    # Suspend
    suspended = await service.suspend(project_id, hook.id, "Drift issue")
    assert suspended.status == "suspended"
    assert suspended.suspended_reason == "Drift issue"

    # Event hook.suspended emitted
    susp_events = await events.history(project_id, types=["hook.suspended"])
    assert len(susp_events) == 1
    assert susp_events[0].subject_id == hook.id
    assert susp_events[0].payload["reason"] == "Drift issue"

    # set_enabled(True) NEVER clears suspension
    enabled = await service.set_enabled(project_id, hook.id, True)
    assert enabled.status == "suspended"
    assert enabled.suspended_reason == "Drift issue"

    # approve clears suspension
    approved = await service.approve(project_id, hook.id, by="operator")
    assert approved.status == "active"
    assert approved.suspended_reason is None


@pytest.mark.asyncio
async def test_set_deploys_verified(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _sample_spec()
    hook = await service.register(project_id, spec, "train", "cancel", "handoff")
    assert hook.deploys_verified is None

    verified = await service.set_deploys_verified(project_id, hook.id, True)
    assert verified.deploys_verified is True

    unverified = await service.set_deploys_verified(project_id, hook.id, False)
    assert unverified.deploys_verified is False


@pytest.mark.asyncio
async def test_list_and_runs_filtering(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec1 = _sample_spec(kind="train")
    spec2 = _sample_spec(kind="deploy")

    h1 = await service.register(project_id, spec1, "train", "cancel", "handoff")
    h2 = await service.register(project_id, spec2, "deploy", None, "handoff")

    # List all
    all_hooks = await service.list(project_id)
    assert len(all_hooks) == 2

    # List by model
    cancel_hooks = await service.list(project_id, model="cancel")
    assert len(cancel_hooks) == 1
    assert cancel_hooks[0].id == h1.id

    # Add runner and run both
    service.add_runner(FakeRunner(runner_type="command"))
    auth = Authority(basis="rule", ref="rul_1", reason="ok", evidence=[])

    await service.run(project_id, HookCall(hook_id=h1.id, reason="run h1"), auth)
    await service.run(project_id, HookCall(hook_id=h2.id, reason="run h2"), auth)

    # Filter runs by kinds
    runs_train = await service.runs(project_id, kinds=["train"])
    assert len(runs_train) == 1
    assert runs_train[0].kind == "train"

    runs_all = await service.runs(project_id)
    assert len(runs_all) == 2
