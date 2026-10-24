import pytest

from mlops_agents.agents.policy.table import PermissionLevel, PolicyDecision, decide
from mlops_agents.agents.tools.base import RiskClass

# Written out from the spec's section 5 table, never derived from the code under test.
EXPECTED: list[tuple[PermissionLevel, RiskClass, PolicyDecision]] = [
    ("suggest", RiskClass.READ, "allow"),
    ("suggest", RiskClass.WRITE_CODE, "deny"),
    ("suggest", RiskClass.RUN, "deny"),
    ("suggest", RiskClass.MESSAGE, "allow"),
    ("suggest", RiskClass.GITHUB, "ask"),
    ("suggest", RiskClass.DEPLOY, "deny"),
    ("ask", RiskClass.READ, "allow"),
    ("ask", RiskClass.WRITE_CODE, "ask"),
    ("ask", RiskClass.RUN, "ask"),
    ("ask", RiskClass.MESSAGE, "allow"),
    ("ask", RiskClass.GITHUB, "ask"),
    ("ask", RiskClass.DEPLOY, "ask"),
    ("autonomous", RiskClass.READ, "allow"),
    ("autonomous", RiskClass.WRITE_CODE, "allow"),
    ("autonomous", RiskClass.RUN, "allow"),
    ("autonomous", RiskClass.MESSAGE, "allow"),
    ("autonomous", RiskClass.GITHUB, "allow"),
    ("autonomous", RiskClass.DEPLOY, "ask"),
    ("bypass", RiskClass.READ, "allow"),
    ("bypass", RiskClass.WRITE_CODE, "allow"),
    ("bypass", RiskClass.RUN, "allow"),
    ("bypass", RiskClass.MESSAGE, "allow"),
    ("bypass", RiskClass.GITHUB, "allow"),
    ("bypass", RiskClass.DEPLOY, "allow"),
]


@pytest.mark.parametrize(("level", "risk", "expected"), EXPECTED)
def test_policy_cell(level: PermissionLevel, risk: RiskClass, expected: PolicyDecision) -> None:
    assert decide(risk, level) == expected


def test_every_cell_is_covered() -> None:
    assert len(EXPECTED) == 4 * len(RiskClass)
    assert {(level, risk) for level, risk, _ in EXPECTED} == {
        (level, risk) for level in ("suggest", "ask", "autonomous", "bypass") for risk in RiskClass
    }
