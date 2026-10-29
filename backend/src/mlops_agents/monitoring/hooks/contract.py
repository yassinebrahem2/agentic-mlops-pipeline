"""Action hooks and pipeline-runs contracts and protocols (I29, I36)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.training.contract import TrainingWindow

HookStatus = Literal["active", "suspended", "disabled"]


class PinnedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str


class HookView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    model: str | None = None
    spec: CommandSpec
    status: HookStatus
    suspended_reason: str | None = None
    credential_set: bool
    pins: list[PinnedFile] = []
    approved_commit: str | None = None
    deploys_verified: bool | None = None
    source: Literal["handoff", "proposal"]
    created: datetime


class HookCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook_id: str
    reason: str
    incident_id: str | None = None
    fix_id: str | None = None
    params: dict[str, str] = {}
    rule_id: str | None = None
    policy_id: str | None = None


class HookResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str
    ci_run_url: str | None = None
    request_id: str | None = None


class HookRefused(Exception):
    """Refusal to run a hook. Reason is user-facing."""


class HookRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    hook_id: str
    kind: str
    model: str | None = None
    incident_id: str | None = None
    fix_id: str | None = None
    rule_id: str | None = None
    policy_id: str | None = None
    reason: str
    basis: Literal["rule", "policy", "approval", "none"]
    basis_ref: str | None = None
    evidence: list[str] = []
    ok: bool | None = None
    detail: str | None = None
    request_id: str | None = None
    started: datetime
    ended: datetime | None = None


class HookRunner(Protocol):
    type: Literal["command", "ci_dispatch"]

    async def run(
        self,
        project_id: str,
        hook: HookView,
        params: Mapping[str, str],
        authority: Authority,
    ) -> HookResult: ...


class HookLifecycle(Protocol):
    async def before(self, project_id: str, hook: HookView, call: HookCall) -> HookCall: ...

    async def after(
        self,
        project_id: str,
        hook: HookView,
        call: HookCall,
        result: HookResult,
    ) -> None: ...


class HookPinner(Protocol):
    async def pins(self, project_id: str, hook: HookView) -> tuple[str, list[PinnedFile]]: ...


class Hooks(Protocol):
    def add_runner(self, runner: HookRunner) -> None: ...

    def add_lifecycle(self, lifecycle: HookLifecycle) -> None: ...

    def set_pinner(self, pinner: HookPinner) -> None: ...

    async def list(self, project_id: str, model: str | None = None) -> list[HookView]: ...

    async def get(self, project_id: str, hook_id: str) -> HookView | None: ...

    async def register(
        self,
        project_id: str,
        spec: CommandSpec,
        name: str,
        model: str | None,
        source: Literal["handoff", "proposal"],
    ) -> HookView: ...

    async def set_enabled(self, project_id: str, hook_id: str, enabled: bool) -> HookView: ...

    async def suspend(self, project_id: str, hook_id: str, reason: str) -> HookView: ...

    async def set_deploys_verified(
        self, project_id: str, hook_id: str, verified: bool
    ) -> HookView: ...

    async def approve(self, project_id: str, hook_id: str, by: str) -> HookView: ...

    async def run(self, project_id: str, call: HookCall, authority: Authority) -> HookResult: ...

    async def runs(
        self,
        project_id: str,
        *,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[HookRunView]: ...


# I36 pipeline-runs types
class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    model: str
    champion_version: str | None = None
    window: TrainingWindow | None = None
    mode: Literal["train", "evaluate"] | None = None
    candidate_version: str | None = None
    threshold: float | None = None


class MissingPlaceholder(HookRefused):
    def __init__(self, placeholder: str) -> None:
        super().__init__(f"Missing placeholder {placeholder!r}")
        self.placeholder = placeholder


class PlaceholderResolver(Protocol):
    async def resolve(self, hook: HookView, ctx: RunContext) -> dict[str, str]: ...


class DispatchRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str
    run_url: str | None = None


class CiDispatcher(Protocol):
    async def dispatch(
        self, project_id: str, workflow: str, inputs: dict[str, str]
    ) -> DispatchRef: ...


class PinCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    changed: list[str]


class AliasCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moved: bool
    detail: str


class HookVerifier(Protocol):
    async def verify_pins(self, project_id: str, hook_id: str) -> PinCheck: ...

    async def check_run(
        self, project_id: str, hook_id: str, request_id: str, window: TrainingWindow
    ) -> AliasCheck: ...
