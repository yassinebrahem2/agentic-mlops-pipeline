"""The action permission policy (I30): the roadmap's deploy table as code."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from mlops_agents.agents.approvals.authority import ActionCall, Authority
from mlops_agents.agents.policy.table import PermissionLevel, PolicyDecision

PROPOSE_BYPASS_OVERRIDES: Final[Mapping[str, PolicyDecision]] = MappingProxyType(
    {"promotion": "allow"}
)


def decide_action(level: PermissionLevel, call: ActionCall, authority: Authority) -> PolicyDecision:
    """Implement the I30 matrix from docs/superpowers/plans/2026-10-29-stage-4-6-overview.md:361-374."""
    kind = call.kind

    if kind in ("deploy", "merge_pr"):
        if level == "suggest":
            return "deny"
        if level in ("ask", "autonomous"):
            return "ask"
        return "allow"

    if kind == "hold":
        if level == "suggest":
            return "ask"
        return "allow"

    if kind in ("train", "evaluate"):
        if call.deploys is not False:
            if (
                call.team_gate_authority is True
                and call.has_production_signal is True
                and authority.basis == "policy"
            ):
                return "deny" if level == "suggest" else "allow"
            return "deny" if level == "suggest" else "ask"
        if authority.basis == "policy":
            return "deny" if level == "suggest" else "allow"
        return "deny" if level == "suggest" else "ask"

    if kind in ("rollback", "rerun", "fall_back"):
        if call.deploys is True:
            if (
                call.team_gate_authority is True
                and call.has_production_signal is True
                and authority.basis == "policy"
            ):
                return "deny" if level == "suggest" else "allow"
            return "deny" if level == "suggest" else "ask"
        if authority.basis == "rule":
            return "deny" if level == "suggest" else "allow"
        if level == "suggest":
            return "deny"
        if level in ("ask", "autonomous"):
            return "ask"
        return "allow"

    raise ValueError(f"Unknown action kind: {kind}")
