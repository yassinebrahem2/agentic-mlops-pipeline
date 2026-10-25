"""The permission policy: the spec's section 5 table as data, the one source of every value."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from mlops_agents.agents.tools.base import RiskClass

PermissionLevel = Literal["suggest", "ask", "autonomous", "bypass"]
PolicyDecision = Literal["allow", "ask", "deny"]

_R = RiskClass
_POLICY: Mapping[PermissionLevel, Mapping[RiskClass, PolicyDecision]] = MappingProxyType(
    {
        "suggest": MappingProxyType(
            {
                _R.READ: "allow",
                _R.WRITE_CODE: "deny",
                _R.RUN: "deny",
                _R.MESSAGE: "allow",
                _R.GITHUB: "ask",
                _R.DEPLOY: "deny",
            }
        ),
        "ask": MappingProxyType(
            {
                _R.READ: "allow",
                _R.WRITE_CODE: "ask",
                _R.RUN: "ask",
                _R.MESSAGE: "allow",
                _R.GITHUB: "ask",
                _R.DEPLOY: "ask",
            }
        ),
        "autonomous": MappingProxyType(
            {
                _R.READ: "allow",
                _R.WRITE_CODE: "allow",
                _R.RUN: "allow",
                _R.MESSAGE: "allow",
                _R.GITHUB: "allow",
                _R.DEPLOY: "ask",
            }
        ),
        "bypass": MappingProxyType(
            {
                _R.READ: "allow",
                _R.WRITE_CODE: "allow",
                _R.RUN: "allow",
                _R.MESSAGE: "allow",
                _R.GITHUB: "allow",
                _R.DEPLOY: "allow",
            }
        ),
    }
)


def decide(risk: RiskClass, level: PermissionLevel) -> PolicyDecision:
    return _POLICY[level][risk]
