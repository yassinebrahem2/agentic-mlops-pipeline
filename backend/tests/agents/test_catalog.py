from pathlib import Path

import pytest

from mlops_agents.agents.catalog import AgentCatalog, DefinitionError, parse_definition
from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.names import STATUS_BOARD_HOOK
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.errors import DomainError

TOOLS = frozenset({"Read", "Glob", "Grep"})
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


def test_status_board_defaults_to_off_and_can_be_switched_on() -> None:
    assert parse_definition(VALID, "helper.md").status_board is False
    on = parse_definition(
        VALID.replace("tools: [Read]", "tools: [Read]\nstatus_board: true"), "h.md"
    )
    assert on.status_board is True


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
        AgentCatalog.load(STAGE_2_TOOLS, [tmp_path], hook_names=frozenset({STATUS_BOARD_HOOK}))


def test_status_board_true_is_the_status_board_hook_and_needs_no_registered_name() -> None:
    definition = parse_definition(_with("status_board: true"), "a.md")
    assert definition.hook_names == (STATUS_BOARD_HOOK,)
    assert AgentCatalog([definition], TOOLS).get("helper").hook_names == ("status_board",)


def test_status_board_listed_and_declared_appears_once() -> None:
    definition = parse_definition(
        _with("context_hooks: [profile, status_board]\nstatus_board: true"), "a.md"
    )
    assert definition.hook_names == ("profile", STATUS_BOARD_HOOK)


class _CountingHook:
    name = STATUS_BOARD_HOOK

    def __init__(self) -> None:
        self.renders = 0

    async def render(self, ctx: HookContext) -> str:
        self.renders += 1
        return "board"


async def test_status_board_listed_and_declared_loads_and_runs_once() -> None:
    """Not a duplicate: the duplicate check counts only `context_hooks`."""
    definition = parse_definition(
        _with("context_hooks: [status_board]\nstatus_board: true"), "a.md"
    )
    catalog = AgentCatalog([definition], TOOLS, hook_names=frozenset({STATUS_BOARD_HOOK}))
    hook = _CountingHook()
    hooks = HookRegistry()
    hooks.register(hook)

    ctx = HookContext(project_id="prj_1", agent="helper", trigger_text="")
    text = await hooks.run(catalog.get("helper").hook_names, ctx)

    assert hook.renders == 1
    assert text == '<data source="hook:status_board">\nboard\n</data>'


def test_an_agent_without_hooks_has_no_hook_names() -> None:
    assert parse_definition(VALID, "a.md").hook_names == ()


def test_load_passes_the_hook_names(tmp_path: Path) -> None:
    (tmp_path / "helper.md").write_text(_with("context_hooks: [profile]"), encoding="utf-8")
    with pytest.raises(DefinitionError, match="unknown context hooks"):
        AgentCatalog.load(STAGE_2_TOOLS, [tmp_path])
    catalog = AgentCatalog.load(STAGE_2_TOOLS, [tmp_path], hook_names=frozenset({"profile"}))
    assert catalog.get("helper").hook_names == ("profile",)


def test_packaged_orchestrator() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    orchestrator = catalog.get("orchestrator")
    assert orchestrator.tools == (
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
    assert orchestrator.chat
    assert catalog.all()[0].name == "orchestrator"
    with pytest.raises(DomainError) as err:
        catalog.get("nobody")
    assert err.value.code == "agent_not_found"


def test_adding_an_agent_is_adding_a_file(tmp_path: Path) -> None:
    (tmp_path / "helper.md").write_text(VALID, encoding="utf-8")
    catalog = AgentCatalog.load(STAGE_2_TOOLS, [tmp_path])
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
