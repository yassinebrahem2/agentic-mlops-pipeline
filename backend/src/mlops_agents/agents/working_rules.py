"""Shared working rules appended to agent system prompts."""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

from mlops_agents.sandbox.containers import (
    SANDBOX_CPUS,
    SANDBOX_DEFAULT_NETWORK,
    SANDBOX_MEMORY,
)

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


def sandbox_limits(*, sandbox_network: bool = SANDBOX_DEFAULT_NETWORK) -> str:
    """Return the sandbox limits section tailored to the sandbox network setting."""
    net_status = "enabled" if sandbox_network else "off"
    if sandbox_network:
        net_bullet = "- Network: enabled (network access is granted)."
        pkg_bullet = (
            "- No system package installs (package installs fail for lack of root and do"
            " not persist past the task's container)."
        )
    else:
        net_bullet = "- No network: network is off unless granted."
        pkg_bullet = (
            "- No system package installs (package installs fail: no network, no root; a"
            " task's container is removed with the task)."
        )
    return (
        f"Sandbox limits ({SANDBOX_CPUS} CPUs, {SANDBOX_MEMORY} memory, network {net_status}):\n"
        f"{net_bullet}\n"
        f"{pkg_bullet}\n"
        "- duckdb is the `duckdb` command (CLI, not Python `import duckdb`).\n"
        "- An environment failure is reported once and not retried in variants."
    )


def working_rules(
    definition: AgentDefinition,
    *,
    sandbox_network: bool = SANDBOX_DEFAULT_NETWORK,
) -> str:
    """Return the hard limits and team system rules tailored to this agent."""
    hard_limits = _load_prompt("hard_limits.md")
    parts = [hard_limits]
    if "OpenPullRequest" in definition.tools:
        parts.append(_load_prompt("team_system.md"))
    else:
        parts.append(_NO_PULL_REQUESTS_LINE)
    if "Bash" in definition.tools:
        parts.append(sandbox_limits(sandbox_network=sandbox_network))
    return "\n\n".join(parts)
