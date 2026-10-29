"""Action authority and gate facts (I30)."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

ActionKind = Literal[
    "train",
    "evaluate",
    "deploy",
    "rollback",
    "rerun",
    "hold",
    "fall_back",
    "merge_pr",
]


class ActionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    agent: str
    kind: ActionKind
    hook_id: str | None = None
    model: str | None = None
    incident_id: str | None = None
    rule_id: str | None = None
    policy_id: str | None = None
    deploys: bool | None = None
    has_production_signal: bool | None = None
    team_gate_authority: bool | None = None


class Authority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: Literal["rule", "policy", "approval", "none"]
    ref: str | None = None
    reason: str
    evidence: list[str] = []


class ActionAuthority(Protocol):
    async def authorize(self, call: ActionCall) -> Authority: ...


class GateFacts(Protocol):
    async def facts(
        self, project_id: str, hook_id: str | None
    ) -> tuple[bool | None, bool | None, bool | None]: ...
