from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.agents.catalog import (
    AgentCatalog,
    AgentDefinition,
    DefinitionError,
    parse_definition,
)
from mlops_agents.agents.tools import default_registry

STAGE_2_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "Bash",
        "StartTask",
        "ResumeTask",
        "OpenPullRequest",
        "SearchIssues",
        "OpenIssue",
        "CommentOnIssue",
        "ListAgents",
        "SendMessage",
        "ScheduleWakeup",
    }
)

EXPECTED_TOOL_ORDER = (
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "Bash",
    "StartTask",
    "ResumeTask",
    "OpenPullRequest",
    "SearchIssues",
    "OpenIssue",
    "CommentOnIssue",
    "ListAgents",
    "SendMessage",
    "ScheduleWakeup",
)

NAME_MAP: dict[str, str] = {
    "Orchestrator": "orchestrator",
    "Onboarding & Diagnosis": "onboarding",
    "Data Quality": "data-quality",
    "Model Performance": "model-performance",
    "Retraining": "retraining",
    "Deployment & Rollback": "deployment",
}


def load_spec_table_data() -> list[dict[str, Any]]:
    spec_path = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-10-24-stage-2-agent-runtime-design.md"
    )
    content = spec_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    in_section_9 = False
    table_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## 9. The seven agents"):
            in_section_9 = True
            continue
        if in_section_9:
            if stripped.startswith("## "):
                break
            if stripped.startswith("|"):
                table_lines.append(stripped)

    # table_lines[0]: header, table_lines[1]: divider
    rows: list[dict[str, Any]] = []
    for row in table_lines[2:]:
        cols = [c.strip() for c in row.split("|")[1:-1]]
        agent_display = cols[0]
        if agent_display not in NAME_MAP:
            continue  # Alert Prioritization is handled in Task 2
        name = NAME_MAP[agent_display]
        chat = cols[1].lower() == "yes"
        schedules = cols[4].lower() == "yes"
        raw_msg = cols[3]
        if raw_msg == "all but Alert Prioritization":
            can_message = (
                "onboarding",
                "data-quality",
                "model-performance",
                "retraining",
                "deployment",
            )
        else:
            can_message = tuple(NAME_MAP[p.strip()] for p in raw_msg.split(","))

        rows.append(
            {
                "name": name,
                "display_name": agent_display,
                "chat": chat,
                "schedules": schedules,
                "status_board": (name == "orchestrator"),
                "tools": EXPECTED_TOOL_ORDER,
                "can_message": can_message,
            }
        )
    return rows


SPEC_ROWS = load_spec_table_data()


def _load_definition(name: str) -> AgentDefinition:
    folder = files("mlops_agents.agents").joinpath("definitions")
    entry = folder.joinpath(f"{name}.md")
    return parse_definition(entry.read_text(encoding="utf-8"), f"{name}.md")


@pytest.mark.parametrize("expected", SPEC_ROWS, ids=[r["name"] for r in SPEC_ROWS])
def test_stage2_agent_definitions_match_spec(expected: dict[str, Any]) -> None:
    defn = _load_definition(expected["name"])

    assert defn.name == expected["name"]
    assert defn.display_name == expected["display_name"]
    assert defn.chat is expected["chat"]
    assert defn.schedules is expected["schedules"]
    assert defn.status_board is expected["status_board"]

    # Read MUST be the first tool
    assert defn.tools[0] == "Read"
    assert defn.tools == expected["tools"]

    # can_message exact peer list
    assert defn.can_message == expected["can_message"]

    # Prompt line count constraint: strictly under 60 lines
    prompt_lines = defn.prompt.splitlines()
    assert len(prompt_lines) < 60, f"{defn.name} prompt exceeds 60 lines: {len(prompt_lines)} lines"

    # Prompt structure: 3 distinct parts
    # 1. The role
    # 2. What the agent can do now (and note on what is not available yet)
    # 3. How to work
    lower_prompt = defn.prompt.lower()
    assert "how to work:" in lower_prompt or "how to work" in lower_prompt, (
        f"{defn.name} prompt missing 'How to work' section"
    )
    assert "starttask" in lower_prompt, f"{defn.name} prompt missing StartTask guidance"
    assert "not available yet" in lower_prompt, (
        f"{defn.name} prompt missing note on what is not available yet"
    )


def test_agent_catalog_loads_stage2_definitions_with_full_tools() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    assert catalog.get("orchestrator").status_board is True
    for name in (
        "onboarding",
        "data-quality",
        "model-performance",
        "retraining",
        "deployment",
    ):
        defn = catalog.get(name)
        assert defn.status_board is False
        assert defn.chat is True
        assert defn.schedules is True


def test_agent_catalog_fails_when_messaging_tools_missing() -> None:
    # default_registry().names() from main does not yet have messaging tools
    with pytest.raises(DefinitionError) as exc_info:
        AgentCatalog.load(default_registry().names())
    error_msg = str(exc_info.value)
    assert "ListAgents" in error_msg
    assert "ScheduleWakeup" in error_msg
    assert "SendMessage" in error_msg
