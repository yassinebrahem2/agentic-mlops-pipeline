"""The doubles of the turn-end sweep's tests: the approval gate ending its step another way once it
has asked (held until released, letting every call run, or failing), and a call held until
released, or only signalled, at the moment a race needs it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Literal

import pytest

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.runtime.hooks import GateCall, GateDecision


class Hold:
    """A call held from `entered` until `release`."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_entered(self) -> None:
        await asyncio.wait_for(self.entered.wait(), timeout=10)


def gate_then(monkeypatch: pytest.MonkeyPatch, then: Literal["hold", "allow", "fail"]) -> Hold:
    """The approval gate asks as usual (its approvals are created), then holds the step until
    released, lets every call run without waiting, or fails the step. Before the turn waits."""
    held = Hold()
    decide = ApprovalGate.decide

    async def asked_then(self: ApprovalGate, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        decisions = await decide(self, calls)
        held.entered.set()
        if then == "fail":
            raise RuntimeError("the gate failed after asking")
        if then == "allow":
            return {call_id: GateDecision(outcome="allow") for call_id in decisions}
        await held.release.wait()
        return decisions

    monkeypatch.setattr(ApprovalGate, "decide", asked_then)
    return held


def hold(
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    name: str,
    when: Callable[..., bool] = lambda *args, **kwargs: True,
) -> Hold:
    """Hold the first call of `owner.name` that `when` matches until released; the others pass."""
    held = Hold()
    original = getattr(owner, name)

    async def held_call(*args: Any, **kwargs: Any) -> Any:
        if not held.entered.is_set() and when(*args, **kwargs):
            held.entered.set()
            await held.release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, name, held_call)
    return held


def signalled(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> asyncio.Event:
    """An event set once `owner.name` is called; the call runs on unchanged."""
    called = asyncio.Event()
    original = getattr(owner, name)

    async def signalling(*args: Any, **kwargs: Any) -> Any:
        called.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, name, signalling)
    return called
