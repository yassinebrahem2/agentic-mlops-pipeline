"""Shared working rules appended to agent system prompts."""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mlops_agents.agents.catalog import AgentDefinition

_NO_PULL_REQUESTS_LINE = (
    "Changes to the team's system go through PRs by the agent whose area it is; "
    "you open none. Your part is to merge duplicate incidents and to move a severity "
    "by at most one level."
)


def _load_prompt(filename: str) -> str:
    folder = files("mlops_agents.agents.prompts")
    return folder.joinpath(filename).read_text(encoding="utf-8").strip()


def working_rules(definition: AgentDefinition) -> str:
    """Return the hard limits and team system rules tailored to this agent."""
    hard_limits = _load_prompt("hard_limits.md")
    if "OpenPullRequest" in definition.tools:
        team_system = _load_prompt("team_system.md")
        return f"{hard_limits}\n\n{team_system}"
    return f"{hard_limits}\n\n{_NO_PULL_REQUESTS_LINE}"
