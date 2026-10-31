from pathlib import Path

import pytest

from mlops_agents.agents.catalog import AgentCatalog, DefinitionError, parse_definition
from mlops_agents.agents.hooks.names import STATUS_BOARD_HOOK
from mlops_agents.agents.tools.names import ADJUST_SEVERITY, RECORD_FIX_CHOICE, SAVE_REPORT
from mlops_agents.errors import DomainError
from tests.conftest import STAGE_2_TOOLS, STAGE_3_HOOKS

TOOLS = frozenset({"Read", "Glob", "Grep"})

VALID = """---
name: helper
display_name: Helper
description: Helps.
tools: [Read]
---
You help.
"""


def test_parse_valid_definition() -> None:
    d = parse_definition(VALID, "helper.md")
    assert (d.name, d.display_name, d.tools, d.chat, d.limits.max_steps) == (
        "helper",
        "Helper",
        ("Read",),
        True,
        25,
    )
    assert d.prompt == "You help."


def test_status_board_key_raises_definition_error() -> None:
    text = VALID.replace("tools: [Read]", "tools: [Read]\nstatus_board: true")
    with pytest.raises(DefinitionError, match="status_board"):
        parse_definition(text, "helper.md")


def test_an_unknown_key_still_fails() -> None:
    text = VALID.replace("tools: [Read]", "tools: [Read]\nstatus_bored: true")
    with pytest.raises(DefinitionError, match="status_bored"):
        parse_definition(text, "helper.md")


def test_windows_line_endings() -> None:
    assert parse_definition(VALID.replace("\n", "\r\n"), "x.md").name == "helper"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: x\n", "must start with"),
        ("---\nname: x\n", "not closed"),
        ("---\n: [\n---\nbody\n", "invalid YAML"),
        ("---\nname: Bad Name\ndisplay_name: X\ndescription: d\n---\nbody\n", "name"),
        ("---\nname: x\ndisplay_name: X\ndescription: d\nmodel: big\n---\nbody\n", "model"),
        ("---\nname: x\ndisplay_name: X\ndescription: d\n---\n\n", "prompt"),
    ],
)
def test_invalid_definitions(text: str, message: str) -> None:
    with pytest.raises(DefinitionError, match=message):
        parse_definition(text, "x.md")


def test_catalog_rejects_unknown_tools_agents_and_hooks() -> None:
    bad_tool = parse_definition(VALID.replace("[Read]", "[Bash]"), "a.md")
    with pytest.raises(DefinitionError, match="unknown tools"):
        AgentCatalog([bad_tool], TOOLS)
    bad_peer = parse_definition(
        VALID.replace("tools: [Read]", "tools: [Read]\ncan_message: [ghost]"), "b.md"
    )
    with pytest.raises(DefinitionError, match="unknown agents"):
        AgentCatalog([bad_peer], TOOLS)
    bad_hook = parse_definition(
        VALID.replace("tools: [Read]", "tools: [Read]\ncontext_hooks: [profile]"), "c.md"
    )
    with pytest.raises(DefinitionError, match="unknown context hooks"):
        AgentCatalog([bad_hook], TOOLS)


def _with(extra: str) -> str:
    return VALID.replace("tools: [Read]", f"tools: [Read]\n{extra}")


def test_context_hooks_are_validated_against_the_registered_names() -> None:
    uses_profile = parse_definition(_with("context_hooks: [profile]"), "a.md")
    catalog = AgentCatalog([uses_profile], TOOLS, hook_names=frozenset({"profile"}))
    assert catalog.get("helper").hook_names == ("profile",)
    with pytest.raises(DefinitionError, match="unknown context hooks: profile"):
        AgentCatalog([uses_profile], TOOLS, hook_names=frozenset({"other"}))


def test_a_context_hook_listed_twice_is_rejected() -> None:
    twice = parse_definition(_with("context_hooks: [profile, status_board, profile]"), "a.md")
    names = frozenset({"profile", STATUS_BOARD_HOOK})
    with pytest.raises(DefinitionError, match="agent helper lists context hooks twice: profile"):
        AgentCatalog([twice], TOOLS, hook_names=names)


def test_load_rejects_a_context_hook_listed_twice(tmp_path: Path) -> None:
    (tmp_path / "helper.md").write_text(
        _with("context_hooks: [status_board, status_board]"), encoding="utf-8"
    )
    with pytest.raises(DefinitionError, match="lists context hooks twice: status_board"):
        AgentCatalog.load(STAGE_2_TOOLS, [tmp_path], hook_names=STAGE_3_HOOKS)


def test_an_agent_without_hooks_has_no_hook_names() -> None:
    assert parse_definition(VALID, "a.md").hook_names == ()


def test_load_passes_the_hook_names(tmp_path: Path) -> None:
    (tmp_path / "helper.md").write_text(_with("context_hooks: [profile]"), encoding="utf-8")
    with pytest.raises(DefinitionError, match="unknown context hooks"):
        AgentCatalog.load(STAGE_2_TOOLS, [tmp_path], hook_names=STAGE_3_HOOKS)
    catalog = AgentCatalog.load(
        STAGE_2_TOOLS, [tmp_path], hook_names=STAGE_3_HOOKS | frozenset({"profile"})
    )
    assert catalog.get("helper").hook_names == ("profile",)


def test_packaged_orchestrator() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    orchestrator = catalog.get("orchestrator")
    assert set(orchestrator.tools) == STAGE_2_TOOLS - {
        "Propose",
        ADJUST_SEVERITY,
        RECORD_FIX_CHOICE,
        SAVE_REPORT,
    }
    assert orchestrator.chat
    assert catalog.all()[0].name == "orchestrator"
    with pytest.raises(DomainError) as err:
        catalog.get("nobody")
    assert err.value.code == "agent_not_found"


def test_adding_an_agent_is_adding_a_file(tmp_path: Path) -> None:
    (tmp_path / "helper.md").write_text(VALID, encoding="utf-8")
    catalog = AgentCatalog.load(STAGE_2_TOOLS, [tmp_path], hook_names=STAGE_3_HOOKS)
    assert [d.name for d in catalog.all()] == [
        "orchestrator",
        "alert-prioritization",
        "data-quality",
        "deployment",
        "helper",
        "model-performance",
        "onboarding",
        "retraining",
    ]
