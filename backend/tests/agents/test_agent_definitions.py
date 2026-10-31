from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any, cast, get_args

import httpx2
import pytest

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import (
    AgentCatalog,
    AgentDefinition,
    DefinitionError,
    parse_definition,
)
from mlops_agents.agents.hooks.data_summary import DataSummaryHook
from mlops_agents.agents.hooks.knowledge import KnowledgeHook
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.hooks.status_board import StatusBoardHook
from mlops_agents.agents.hooks.tracking_summary import TrackingSummaryHook
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import default_registry, names
from mlops_agents.agents.tools.names import (
    ADJUST_SEVERITY,
    HOLD,
    INCIDENT_FOLLOW_UP,
    INCIDENT_MERGE,
    INCIDENT_SPLIT,
    INCIDENT_UPDATE,
    MERGE_PULL_REQUEST,
    MONITORING_QUERY,
    RECORD_FIX_CHOICE,
    REGISTER_CHECK,
    REQUEST_FIX,
    RUN_HOOK,
    SAVE_REPORT,
)
from mlops_agents.agents.tools.propose import ProposeTool
from mlops_agents.agents.tools.pull_request import OpenPullRequestTool
from mlops_agents.agents.tools.save_note import SaveNote
from mlops_agents.agents.working_rules import working_rules
from mlops_agents.config import RunMode, load_settings
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.status import SqliteStatusBoard
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.monitoring.hooks.constants import HOOK_CREDENTIAL_ENV
from mlops_agents.monitoring.incidents.contract import WakeLabel, diagnoser
from mlops_agents.monitoring.results.contract import ResultCategory, ResultOrigin, ResultRecord
from mlops_agents.monitoring.results.parser_kind import CODE_PARSER_CONTRACT
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.paths import DataPaths
from mlops_agents.promotion.constants import REQUIRED_REPORT_FIELDS
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    HANDOFF_KIND,
    HOOKS_KIND,
    MAINTENANCE_KIND,
    PARSER_KIND,
    PROMOTION_KIND,
    RULE_KIND,
    TRAINING_POLICY_KIND,
    HandoffPayload,
)
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import ModelSpec, Operations
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.training.constants import REQUEST_TAG
from mlops_agents.training.contract import FixChoice
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.builder_env import status_board_hooks
from tests.agents.runtime.hook_fakes import NoInbound
from tests.conftest import (
    STAGE_2_TOOLS,
    STAGE_3_HOOKS,
    MemoryKeyStore,
    make_repo,
    memory_project_secrets,
)
from tests.fakes.turn_end import NoTurnEndCheck
from tests.integrations.test_github_auth import FakeGh

NAME_MAP: dict[str, str] = {
    "Orchestrator": "orchestrator",
    "Onboarding & Diagnosis": "onboarding",
    "Data Quality": "data-quality",
    "Model Performance": "model-performance",
    "Retraining": "retraining",
    "Deployment & Rollback": "deployment",
    "Alert Prioritization": "alert-prioritization",
}


def load_spec_table_data() -> list[dict[str, Any]]:
    spec_path = (
        Path(__file__).resolve().parents[3]
        / "docs/superpowers/specs/2026-10-24-stage-2-agent-runtime-design.md"
    )
    lines = spec_path.read_text(encoding="utf-8").splitlines()
    in_s9 = False
    table_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("## 9. The seven agents"):
            in_s9 = True
            continue
        if in_s9:
            if s.startswith("## "):
                break
            if s.startswith("|"):
                table_lines.append(s)

    data_lines = table_lines[2:]
    if len(data_lines) != 7:
        raise AssertionError(f"Expected exactly 7 rows in spec table, got {len(data_lines)}")

    parsed_rows: list[dict[str, Any]] = []
    for row in data_lines:
        cols = [c.strip() for c in row.split("|")[1:-1]]
        agent_display = cols[0]
        if agent_display not in NAME_MAP:
            raise AssertionError(f"Unmapped agent display name: {agent_display!r}")
        chat_val = cols[1].lower()
        if chat_val not in ("yes", "no"):
            raise AssertionError(f"Chat value must be yes/no for {agent_display}, got {cols[1]!r}")
        wake_val = cols[4].lower()
        if wake_val not in ("yes", "no"):
            raise AssertionError(f"Wake-ups must be yes/no for {agent_display}, got {cols[4]!r}")
        parsed_rows.append(
            {
                "display_name": agent_display,
                "name": NAME_MAP[agent_display],
                "chat": chat_val == "yes",
                "raw_tools": cols[2],
                "raw_msg": cols[3],
                "schedules": wake_val == "yes",
                "status_board": (NAME_MAP[agent_display] == "orchestrator"),
            }
        )

    all_agents = [r["name"] for r in parsed_rows]
    chat_agents = [r["name"] for r in parsed_rows if r["chat"]]

    result: list[dict[str, Any]] = []
    prev_tools: tuple[str, ...] = ()
    for r in parsed_rows:
        raw_tools = r["raw_tools"]
        if raw_tools.lower() == "same":
            tools = prev_tools
        elif "all general tools" in raw_tools.lower():
            base = ("Read", "Glob", "Grep", "Write", "Edit", "Bash", "StartTask", "ResumeTask")
            rest = (
                "OpenPullRequest",
                "SearchIssues",
                "OpenIssue",
                "CommentOnIssue",
                "ListAgents",
                "SendMessage",
            )
            tools = base + rest + (("ScheduleWakeup",) if r["schedules"] else ())
        else:
            tools = tuple(t.strip() for t in raw_tools.split(",") if t.strip())

        if not tools or tools[0] != "Read":
            raise AssertionError(f"Tools for {r['name']} must start with 'Read', got {tools}")
        prev_tools = tools

        raw_msg = r["raw_msg"]
        if raw_msg == "all but Alert Prioritization":
            can_msg = tuple(a for a in all_agents if a not in ("alert-prioritization", r["name"]))
        elif raw_msg == "all chat agents":
            can_msg = tuple(a for a in chat_agents if a != r["name"])
        else:
            for p in raw_msg.split(","):
                p_clean = p.strip()
                if p_clean and p_clean not in NAME_MAP:
                    raise AssertionError(f"Unmapped agent in May message: {p_clean!r}")
            can_msg = tuple(NAME_MAP[p.strip()] for p in raw_msg.split(",") if p.strip())
        result.append({**r, "tools": tools, "can_message": can_msg})
    return result


SPEC_ROWS = load_spec_table_data()


def _load_definition(name: str) -> AgentDefinition:
    folder = files("mlops_agents.agents").joinpath("definitions")
    entry = folder.joinpath(f"{name}.md")
    return parse_definition(entry.read_text(encoding="utf-8"), f"{name}.md")


STAGE_3_TABLE: list[dict[str, Any]] = [
    {
        "name": "orchestrator",
        "display_name": "Orchestrator",
        "chat": True,
        "schedules": True,
        "tools": (
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
            "SaveNote",
            "SearchNotes",
            "TrackingHistory",
            "ReadHandoff",
        ),
        "context_hooks": ("status_board", "tracking_summary", "knowledge"),
        "max_steps": 25,
        "can_message": (
            "onboarding",
            "data-quality",
            "model-performance",
            "retraining",
            "deployment",
        ),
    },
    {
        "name": "onboarding",
        "display_name": "Onboarding & Diagnosis",
        "chat": True,
        "schedules": True,
        "tools": (
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
            "SaveNote",
            "SearchNotes",
            "TrackingHistory",
            "ReadHandoff",
            "Propose",
        ),
        "context_hooks": ("data_summary", "knowledge"),
        "max_steps": 60,
        "can_message": ("orchestrator", "data-quality", "model-performance"),
    },
    {
        "name": "data-quality",
        "display_name": "Data Quality",
        "chat": True,
        "schedules": True,
        "tools": (
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
            "SaveNote",
            "SearchNotes",
            "ReadHandoff",
        ),
        "context_hooks": ("data_summary", "knowledge"),
        "max_steps": 25,
        "can_message": ("orchestrator", "model-performance", "retraining"),
    },
    {
        "name": "model-performance",
        "display_name": "Model Performance",
        "chat": True,
        "schedules": True,
        "tools": (
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
            "SaveNote",
            "SearchNotes",
            "TrackingHistory",
            "ReadHandoff",
        ),
        "context_hooks": ("data_summary", "tracking_summary", "knowledge"),
        "max_steps": 25,
        "can_message": ("orchestrator", "data-quality", "retraining"),
    },
    {
        "name": "retraining",
        "display_name": "Retraining",
        "chat": True,
        "schedules": True,
        "tools": (
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
            "SaveNote",
            "SearchNotes",
            "TrackingHistory",
            "ReadHandoff",
        ),
        "context_hooks": ("data_summary", "tracking_summary", "knowledge"),
        "max_steps": 25,
        "can_message": ("orchestrator", "model-performance", "deployment"),
    },
    {
        "name": "deployment",
        "display_name": "Deployment & Rollback",
        "chat": True,
        "schedules": True,
        "tools": (
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
            "SaveNote",
            "SearchNotes",
            "TrackingHistory",
            "ReadHandoff",
        ),
        "context_hooks": ("tracking_summary", "knowledge"),
        "max_steps": 25,
        "can_message": ("orchestrator", "retraining", "model-performance"),
    },
    {
        "name": "alert-prioritization",
        "display_name": "Alert Prioritization",
        "chat": False,
        "schedules": False,
        "tools": ("Read", "Glob", "Grep"),
        "context_hooks": (),
        "max_steps": 25,
        "can_message": (),
    },
]

# The tool matrix of the stages 4-6 definitions plan: the stage 3 rows above plus one column
# per task done so far. D = the agents `diagnoser()` can return.
DIAGNOSING_AGENTS: tuple[str, ...] = (
    "orchestrator",
    "data-quality",
    "model-performance",
    "retraining",
    "deployment",
)
DIAGNOSING_TOOLS: tuple[str, ...] = (
    INCIDENT_UPDATE,
    INCIDENT_MERGE,
    INCIDENT_SPLIT,
    INCIDENT_FOLLOW_UP,
    MONITORING_QUERY,
    RUN_HOOK,
    HOLD,
)
TASK_1_COLUMN: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(DIAGNOSING_AGENTS, DIAGNOSING_TOOLS),
    "alert-prioritization": (MONITORING_QUERY, INCIDENT_MERGE, INCIDENT_UPDATE, ADJUST_SEVERITY),
}
TASK_2_COLUMN: dict[str, tuple[str, ...]] = {
    "data-quality": (REQUEST_FIX, ProposeTool.name),
    "model-performance": (REQUEST_FIX, ProposeTool.name),
    "retraining": (RECORD_FIX_CHOICE, SAVE_REPORT, ProposeTool.name),
    "deployment": (REQUEST_FIX, ProposeTool.name),
    "orchestrator": (REQUEST_FIX,),
}
# Every holder of `OpenPullRequest` gains `MergePullRequest`; the four agents that write
# checks (S4 §4) gain `RegisterCheck`.
CHECK_WRITERS: tuple[str, ...] = ("data-quality", "model-performance", "retraining", "deployment")
TASK_3_COLUMN: dict[str, tuple[str, ...]] = {
    row["name"]: (MERGE_PULL_REQUEST,) + ((REGISTER_CHECK,) if row["name"] in CHECK_WRITERS else ())
    for row in STAGE_3_TABLE
    if OpenPullRequestTool.name in row["tools"]
}
TASK_COLUMNS: tuple[dict[str, tuple[str, ...]], ...] = (
    TASK_1_COLUMN,
    TASK_2_COLUMN,
    TASK_3_COLUMN,
)
AGENT_TABLE: list[dict[str, Any]] = [
    {
        **row,
        "tools": row["tools"] + tuple(t for col in TASK_COLUMNS for t in col.get(row["name"], ())),
    }
    for row in STAGE_3_TABLE
]

# The operational subtypes `diagnoser()` maps to an agent of their own.
DIAGNOSER_SUBTYPES: tuple[str, ...] = ("failed_training", "bad_deploy", "failed_scoring")

# How a diagnosing prompt names each wake label it is woken by.
WAKE_LABEL_WORDS: dict[WakeLabel, str] = {
    "incident": "`incident`",
    "follow_up": "`follow_up`",
}

# S4 §6's response menu, safest first, as each diagnosing prompt words it.
RESPONSE_MENU: tuple[str, ...] = (
    f"`{INCIDENT_FOLLOW_UP}`",
    "notify",
    "open an issue",
    f"`{HOLD}`",
    "roll back",
    "fall back",
    "rerun",
    f"`{RUN_HOOK}`",
    "revert",
)
RESPONSE_MENU_HEADING = "response menu, safest first"

# Every tool name of agents/tools/names.py, read from the module, never retyped.
_NAMED_TOOLS: tuple[str, ...] = tuple(
    value for key, value in vars(names).items() if key.isupper() and isinstance(value, str)
)


def _assert_definition_matches_spec(defn: AgentDefinition, expected: dict[str, Any]) -> None:
    assert defn.name == expected["name"] and defn.display_name == expected["display_name"]
    assert defn.chat is expected["chat"] and defn.schedules is expected["schedules"]
    assert defn.tools[0] == "Read" and defn.tools == expected["tools"]
    assert defn.context_hooks == expected["context_hooks"]
    assert defn.limits.max_steps == expected["max_steps"]
    assert defn.can_message == expected["can_message"]


@pytest.mark.parametrize("expected", AGENT_TABLE, ids=[str(r["name"]) for r in AGENT_TABLE])
def test_agent_definitions_match_the_tool_matrix(expected: dict[str, Any]) -> None:
    defn = _load_definition(str(expected["name"]))
    _assert_definition_matches_spec(defn, expected)
    low = defn.prompt.lower()
    assert "how to work:" in low or "how to work" in low, f"{defn.name} missing 'How to work'"
    if defn.chat:
        assert "starttask" in low, f"{defn.name} missing StartTask guidance"
    else:
        assert "monitoring loop" in low or "stage 4" in low, f"{defn.name} missing loop guidance"


def test_spec_table_detects_missing_tool() -> None:
    expected = AGENT_TABLE[0]
    defn = _load_definition(str(expected["name"]))
    tampered = defn.model_copy(update={"tools": defn.tools[:-1]})
    with pytest.raises(AssertionError):
        _assert_definition_matches_spec(tampered, expected)


def test_all_seven_definitions_load_with_real_registries() -> None:
    class DummySvc:
        pass

    dummy = DummySvc()
    tool_reg = default_registry(
        tasks=cast(Any, dummy),
        sandbox=cast(Any, dummy),
        bus=cast(Any, dummy),
        wakeups=cast(Any, dummy),
        workspace=cast(Any, dummy),
        github=cast(Any, dummy),
        events=cast(Any, dummy),
        http=cast(Any, dummy),
        databases=cast(Any, dummy),
        knowledge=cast(Any, dummy),
        proposals=cast(Any, dummy),
        proposal_kinds=ProposalKinds(),
        tracking=cast(Any, dummy),
        handoffs=cast(Any, dummy),
        reports=cast(Any, dummy),
        fixes=cast(Any, dummy),
        hooks=cast(Any, dummy),
        incidents=cast(Any, dummy),
        severity=cast(Any, dummy),
        query_providers=cast(Any, dummy),
        data_summaries=cast(Any, dummy),
        connections=cast(Any, dummy),
        surfaces=cast(Any, dummy),
        pr_sync=cast(Any, dummy),
        checks=cast(Any, dummy),
    )
    hook_reg = HookRegistry()
    hook_reg.register(StatusBoardHook())
    hook_reg.register(KnowledgeHook(cast(Any, dummy)))
    hook_reg.register(DataSummaryHook(cast(Any, dummy), cast(Any, dummy)))
    hook_reg.register(TrackingSummaryHook(cast(Any, dummy)))

    catalog = AgentCatalog.load(tool_reg.names(), hook_names=hook_reg.names())
    assert len(catalog.all()) == 7


def test_only_orchestrator_has_status_board_hook() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    sb_agents = [d.name for d in catalog.all() if "status_board" in d.context_hooks]
    assert sb_agents == ["orchestrator"]


def test_parse_definition_with_status_board_raises_definition_error() -> None:
    text = """---
name: test-agent
display_name: Test Agent
description: A test agent
tools: [Read]
status_board: true
---
Prompt body.
"""
    with pytest.raises(DefinitionError):
        parse_definition(text, "test.md")


def test_prompt_checks_guard_nothing_it_cannot_do_yet() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for defn in catalog.all():
        # No prompt names a tool the agent lacks
        for tool_name in STAGE_2_TOOLS:
            if tool_name not in defn.tools:
                assert tool_name not in defn.prompt, (
                    f"Agent '{defn.name}' prompt names missing tool '{tool_name}'"
                )

        # No prompt names, in backticks, a tool of names.py the agent lacks
        for tool_name in _NAMED_TOOLS:
            if tool_name not in defn.tools:
                assert f"`{tool_name}`" not in defn.prompt, (
                    f"Agent '{defn.name}' prompt names missing tool '{tool_name}'"
                )

        # ReadHandoff is named by the six agents that have it and not by Alert Prioritization
        if defn.name != "alert-prioritization":
            assert "ReadHandoff" in defn.prompt, f"Agent '{defn.name}' prompt missing ReadHandoff"
        else:
            assert "ReadHandoff" not in defn.prompt

        # The four audit agents' prompts mention "Handoff"
        if defn.name in ("data-quality", "model-performance", "retraining", "deployment"):
            assert "Handoff" in defn.prompt, f"Audit agent '{defn.name}' prompt missing 'Handoff'"


ONBOARDING_FIELD_GROUPS = [
    "models",
    "profile",
    "predictions",
    "labels",
    "promotion",
    "environments",
    "production signal",
    "placeholders",
    "entry files",
    "commands",
    "data roles",
    "edges",
    "feeds",
    "grace",
    "time zone",
    "on call",
    "escalation",
    "checks",
    "results location",
    "gaps",
    "grants",
]


def test_onboarding_coverage_field_groups_and_literal_braces() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    onb_prompt = catalog.get("onboarding").prompt
    low = onb_prompt.lower()
    for group in ONBOARDING_FIELD_GROUPS:
        assert group in low, f"Onboarding prompt missing field name group: '{group}'"
    assert "{{" in onb_prompt, "Onboarding prompt missing literal brace rule '{{'"


FIELD_MAP: dict[str, list[str]] = {
    "models": ["models", "model"],
    "commands": ["commands"],
    "data_roles": ["data roles"],
    "tracking": ["tracking"],
    "pipeline": ["pipeline"],
    "edges": ["edges"],
    "operations": ["operations"],
    "gaps": ["gaps"],
    "grants": ["grants"],
    "name": ["model"],
    "profile": ["profile"],
    "predictions": ["predictions"],
    "labels": ["labels"],
    "promotion": ["promotion"],
    "environments": ["environments"],
    "production_signal": ["production signal"],
    "scheduler": ["scheduler"],
    "feeds": ["feeds"],
    "on_call": ["on call"],
    "checks": ["checks"],
    "results_location": ["results location"],
}


def test_onboarding_coverage_contract_fields() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    prompt_low = catalog.get("onboarding").prompt.lower()
    fields_to_check: list[str] = (
        list(HandoffPayload.model_fields.keys())
        + list(ModelSpec.model_fields.keys())
        + list(Operations.model_fields.keys())
    )
    for field in fields_to_check:
        expected_words = FIELD_MAP.get(field, [field.replace("_", " ")])
        assert any(w in prompt_low for w in expected_words), (
            f"Onboarding prompt missing coverage for field '{field}'"
        )


def test_alert_prioritization_loads_in_catalog() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    defn = catalog.get("alert-prioritization")
    alert_spec = next(r for r in AGENT_TABLE if r["name"] == "alert-prioritization")
    assert defn.name == "alert-prioritization" and defn.display_name == "Alert Prioritization"
    assert not defn.chat and not defn.schedules
    assert defn.tools == alert_spec["tools"] and defn.can_message == alert_spec["can_message"]


REMOVED_FROM_ALERT_PRIORITIZATION: tuple[str, ...] = (
    "ListAgents",
    "SendMessage",
    "SearchIssues",
    "OpenIssue",
    "CommentOnIssue",
)


def test_alert_prioritization_holds_only_the_seven_tools_it_uses() -> None:
    defn = _load_definition("alert-prioritization")
    assert set(defn.tools) == {
        "Read",
        "Glob",
        "Grep",
        MONITORING_QUERY,
        INCIDENT_MERGE,
        INCIDENT_UPDATE,
        ADJUST_SEVERITY,
    }
    assert len(defn.tools) == 7
    for tool_name in REMOVED_FROM_ALERT_PRIORITIZATION:
        assert tool_name not in defn.tools, f"{tool_name} came back to alert-prioritization"
    assert defn.can_message == ()


def test_every_agent_diagnoser_returns_holds_the_diagnosing_tools() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    returned: set[str] = set()
    for category in get_args(ResultCategory):
        for subtype in (None, *DIAGNOSER_SUBTYPES):
            returned.add(diagnoser(category, subtype))
    assert returned == set(DIAGNOSING_AGENTS)
    for name in returned:
        tools = catalog.get(name).tools
        assert set(DIAGNOSING_TOOLS) <= set(tools), f"{name} lacks a diagnosing tool"
    holders = [d.name for d in catalog.all() if ADJUST_SEVERITY in d.tools]
    assert holders == ["alert-prioritization"]


def test_task_tools_are_named_in_their_holders_prompts() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for column in TASK_COLUMNS:
        for name, tools in column.items():
            prompt = catalog.get(name).prompt
            for tool_name in tools:
                assert f"`{tool_name}`" in prompt, f"{name} prompt does not name `{tool_name}`"


def test_merge_pull_request_holders_are_the_open_pull_request_holders() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    mergers = {d.name for d in catalog.all() if MERGE_PULL_REQUEST in d.tools}
    openers = {d.name for d in catalog.all() if OpenPullRequestTool.name in d.tools}
    assert mergers == openers and mergers


# The matrix's last column: the prompts that name each kind as `` `<kind>` proposal ``.
KIND_PROMPTS: dict[str, frozenset[str]] = {
    CONNECTIONS_KIND: frozenset({"onboarding"}),
    HANDOFF_KIND: frozenset({"onboarding"}),
    PROMOTION_KIND: frozenset({"retraining"}),
    MAINTENANCE_KIND: frozenset({"data-quality"}),
    RULE_KIND: frozenset({"model-performance", "deployment"}),
    HOOKS_KIND: frozenset({"deployment", "onboarding"}),
    TRAINING_POLICY_KIND: frozenset({"retraining"}),
    PARSER_KIND: frozenset({"data-quality", "onboarding"}),
}


@pytest.mark.parametrize("kind", list(KIND_PROMPTS))
def test_each_proposal_kind_is_named_by_exactly_its_prompts(kind: str) -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    namers = {d.name for d in catalog.all() if f"`{kind}` proposal" in d.prompt}
    assert namers == KIND_PROMPTS[kind]
    for name in namers:
        assert ProposeTool.name in catalog.get(name).tools, f"{name} names a kind without Propose"


# Task 1's gate wording, false once rules and policies are cited: the gate may allow at once.
STALE_GATE_WORDING = "the gate decides and may ask a person first"


@pytest.mark.parametrize("name", DIAGNOSING_AGENTS)
def test_diagnosing_prompt_cites_rules_and_never_certifies_them(name: str) -> None:
    prompt = _load_definition(name).prompt
    assert "`rule_id`" in prompt, f"{name} prompt does not cite a rule with `rule_id`"
    assert "never certify" in prompt.lower(), f"{name} prompt lets the agent certify a rule"
    assert STALE_GATE_WORDING not in prompt.lower(), f"{name} prompt keeps the stale gate line"


# The record fields and values the check lines name, each pinned to the result record model
# (monitoring/results/contract.py), so a rename there fails here.
CHECK_RECORD_FIELDS: tuple[str, ...] = ("check_id", "version", "not_enough_data")
AGENT_ORIGIN: ResultOrigin = "agent"
# The category confirmation by labels reads as label evidence (monitoring/verdicts/store.py).
LABEL_EVIDENCE_CATEGORY: ResultCategory = "model_performance"
LABEL_EVIDENCE_WORDS = "label evidence"


def test_check_record_names_are_the_record_model_s() -> None:
    assert set(CHECK_RECORD_FIELDS) | {"origin", "category"} <= set(ResultRecord.model_fields)
    assert AGENT_ORIGIN in get_args(ResultOrigin)
    assert LABEL_EVIDENCE_CATEGORY in get_args(ResultCategory)


@pytest.mark.parametrize("name", ["data-quality", "model-performance"])
def test_check_prompts_name_not_enough_data(name: str) -> None:
    prompt = _load_definition(name).prompt
    assert "not enough data" in prompt.lower(), f"{name} prompt lacks the minimum sample line"
    assert f"`{CHECK_RECORD_FIELDS[-1]}`" in prompt


def test_check_writers_describe_the_check_record() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    holders = {d.name for d in catalog.all() if REGISTER_CHECK in d.tools}
    assert holders == set(CHECK_WRITERS)
    for name in CHECK_WRITERS:
        prompt = catalog.get(name).prompt
        for words in (
            *(f"`{field}`" for field in CHECK_RECORD_FIELDS[:-1]),
            f'`origin: "{AGENT_ORIGIN}"`',
            "scheduler",
            "minimum sample",
        ):
            assert words in prompt, f"{name} check line lacks {words}"


@pytest.mark.parametrize("name", CHECK_WRITERS)
def test_check_writers_category_rule(name: str) -> None:
    prompt = _load_definition(name).prompt
    named = [c for c in get_args(ResultCategory) if f"`{c}`" in prompt]
    assert set(named) - {LABEL_EVIDENCE_CATEGORY}, f"{name} check line names no category"
    assert LABEL_EVIDENCE_WORDS in prompt, f"{name} check line does not explain label evidence"
    if name == "model-performance":
        assert f"`{LABEL_EVIDENCE_CATEGORY}`, with the model it judges, only for a label-based" in (
            prompt
        )
    else:
        assert f"never `{LABEL_EVIDENCE_CATEGORY}`" in prompt, f"{name} may file label evidence"


def test_model_performance_first_audit_adds_the_label_based_checks() -> None:
    low = _load_definition("model-performance").prompt.lower()
    assert "label-based checks" in low and "first audit" in low


# Lines that said the stage 4 checks and evaluation runs were still to come.
STALE_LATER_WORDING: tuple[str, ...] = ("arrive in stage 4", "arrive in a later stage")


def test_no_prompt_says_checks_arrive_later() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for defn in catalog.all():
        for words in STALE_LATER_WORDING:
            assert words not in defn.prompt.lower(), f"{defn.name} prompt still says '{words}'"


def _words(text: str) -> str:
    return " ".join(text.lower().split())


# CODE_PARSER_CONTRACT has one source (parser_kind.py). Every run of six words of each of its
# sentences and list items, read from the constant, so a prompt that retypes part of one is
# caught.
CONTRACT_FRAGMENT_WORDS = 6


def _contract_fragments() -> tuple[str, ...]:
    fragments: list[str] = []
    for line in CODE_PARSER_CONTRACT.splitlines():
        for piece in line.removeprefix("- ").split(". "):
            words = _words(piece).split()
            fragments.extend(
                " ".join(words[at : at + CONTRACT_FRAGMENT_WORDS])
                for at in range(len(words) - CONTRACT_FRAGMENT_WORDS + 1)
            )
    return tuple(dict.fromkeys(fragments))


CONTRACT_FRAGMENTS = _contract_fragments()


def _retyped_contract_fragments(prompt: str) -> list[str]:
    text = _words(prompt)
    return [fragment for fragment in CONTRACT_FRAGMENTS if fragment in text]


def test_no_prompt_retypes_the_code_parser_contract() -> None:
    assert len(CONTRACT_FRAGMENTS) >= 100
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for defn in catalog.all():
        found = _retyped_contract_fragments(defn.prompt)
        assert not found, f"{defn.name} prompt retypes the code-parser contract: {found}"


def test_retyped_contract_fragment_is_detected() -> None:
    prompt = _load_definition("data-quality").prompt
    tampered = f"{prompt}\n- {CONTRACT_FRAGMENTS[-1]}"
    assert _retyped_contract_fragments(tampered) == [CONTRACT_FRAGMENTS[-1]]


def test_retraining_cites_an_approved_training_policy() -> None:
    prompt = _load_definition("retraining").prompt
    assert "`policy_id`" in prompt


@pytest.mark.parametrize("name", DIAGNOSING_AGENTS)
def test_diagnosing_prompt_covers_wake_menu_and_knowledge_note(name: str) -> None:
    prompt = _load_definition(name).prompt
    low = prompt.lower()
    assert set(WAKE_LABEL_WORDS) <= set(get_args(WakeLabel))
    for label, words in WAKE_LABEL_WORDS.items():
        assert words in prompt, f"{name} prompt does not name the {label} wake label"
    position = low.find(RESPONSE_MENU_HEADING)
    assert position >= 0, f"{name} prompt has no '{RESPONSE_MENU_HEADING}'"
    menu = RESPONSE_MENU
    if REQUEST_FIX in _load_definition(name).tools:
        # S4 §6's item 6, requesting a fix, comes between the hooks and the revert.
        at = menu.index("revert")
        menu = (*menu[:at], f"`{REQUEST_FIX}`", *menu[at:])
    for item in menu:
        found = low.find(item.lower(), position)
        assert found > position, f"{name} menu lacks '{item}' after the previous item"
        position = found
    assert "knowledge note" in low and f"`{SaveNote.name}`" in prompt
    assert HOOK_CREDENTIAL_ENV in prompt, f"{name} prompt lacks {HOOK_CREDENTIAL_ENV}"


def test_data_quality_and_model_performance_role_lines() -> None:
    dq = _load_definition("data-quality").prompt.lower()
    assert "bad data" in dq and "drift" in dq
    mp = _load_definition("model-performance").prompt.lower()
    assert "label delay" in mp and "skew" in mp


# How Retraining's prompt words each required assessment of the report (I37). Indexed, never
# looked up with a default, so a field contracts adds fails here until the prompt covers it.
REPORT_FIELD_WORDS: dict[str, str] = {
    "fix_chosen": "fix chosen",
    "data_window": "data window",
    "leakage": "leakage",
    "label_maturity": "label maturity",
    "imbalance": "imbalance",
    "training_serving_skew": "training-serving skew",
    "risks_and_watch": "what to watch",
    "rollback_plan": "rollback plan",
}


def test_retraining_prompt_covers_the_fix_menu_and_the_report() -> None:
    prompt = _load_definition("retraining").prompt
    low = prompt.lower()
    for choice in get_args(FixChoice):
        assert f"`{choice}`" in prompt, f"retraining prompt does not name the `{choice}` fix"
    for field in REQUIRED_REPORT_FIELDS:
        words = REPORT_FIELD_WORDS[field]
        assert words in low, f"retraining prompt does not cover the report's {field}"
    assert "`fix_request`" in prompt
    assert "circuit" in low and "escalate" in low
    assert REQUEST_TAG in prompt and "links" in low and "request id" in low
    assert f"`{PROMOTION_KIND}` proposal" in prompt


def test_deployment_and_orchestrator_role_lines() -> None:
    prompt = _load_definition("deployment").prompt
    low = prompt.lower()
    assert "`post_deploy`" in prompt
    for words in ("parity", "holdout", "rollout", "heartbeat"):
        assert words in low, f"deployment prompt does not name {words}"
    assert "silence" in _load_definition("orchestrator").prompt.lower()


def test_every_wake_label_is_named_by_a_prompt() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    prompts = [d.prompt for d in catalog.all()]
    for label in get_args(WakeLabel):
        assert any(f"`{label}`" in p for p in prompts), f"no prompt names the {label} wake label"


def test_alert_prioritization_prompt_leaves_paging_and_routing_to_code() -> None:
    low = _load_definition("alert-prioritization").prompt.lower()
    assert "monitoring loop" in low
    for stage_2_wording in ("does not run", "route alerts", "sendmessage", "not available yet"):
        assert stage_2_wording not in low, f"AP prompt still says '{stage_2_wording}'"


def test_working_rules_describe_the_untrusted_data_block() -> None:
    # The opening tag up to its quoted value: hard_limits.md writes it as <data source=...>.
    opening = untrusted("x", source="t").split('"', 1)[0]
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for defn in catalog.all():
        assert opening in working_rules(defn), f"{defn.name}'s working rules lack {opening}"


async def test_alert_prioritization_derived_as_inactive_on_status_board(
    databases: Databases,
) -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    events = SqliteEventLog(databases)
    board = SqliteStatusBoard(catalog, events)
    statuses = await board.snapshot("prj_test123")
    alert_status = next(s for s in statuses if s.agent == "alert-prioritization")
    assert alert_status.state == "inactive"
    assert alert_status.display_name == "Alert Prioritization"
    summary = await board.summary("prj_test123")
    assert "Alert Prioritization: inactive" in summary


def test_no_stage2_agent_may_message_alert_prioritization() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for defn in catalog.all():
        assert "alert-prioritization" not in defn.can_message, (
            f"Agent '{defn.name}' unexpectedly allows messaging alert-prioritization"
        )


def test_agent_catalog_fails_when_messaging_tools_missing() -> None:
    with pytest.raises(DefinitionError) as exc_info:
        AgentCatalog.load(STAGE_2_TOOLS - {"ListAgents", "SendMessage"}, hook_names=STAGE_3_HOOKS)
    error_msg = str(exc_info.value)
    assert "ListAgents" in error_msg and "SendMessage" in error_msg

    with pytest.raises(DefinitionError) as exc_info_sched:
        AgentCatalog.load(STAGE_2_TOOLS - {"ScheduleWakeup"}, hook_names=STAGE_3_HOOKS)
    assert "ScheduleWakeup" in str(exc_info_sched.value)


def _load_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                clean_k = k.strip()
                clean_v = v.strip().strip("'\"")
                if clean_k not in os.environ:
                    monkeypatch.setenv(clean_k, clean_v)


_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


@pytest.mark.live
@pytest.mark.timeout(300)
async def test_live_agent_definitions(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_local_env(monkeypatch)
    live_model = os.environ.get("MLOPS_AGENTS_LIVE_MODEL")
    if not live_model:
        pytest.skip("set MLOPS_AGENTS_LIVE_MODEL to run this live test")

    provider_name, separator, wire_id = live_model.partition(":")
    if not separator or not wire_id:
        pytest.fail(
            "MLOPS_AGENTS_LIVE_MODEL must be 'provider:model', e.g. groq:llama-3.3-70b-versatile"
        )
    base_url = _BASE_URLS.get(provider_name)
    if base_url is None:
        pytest.fail(f"MLOPS_AGENTS_LIVE_MODEL provider must be one of {sorted(_BASE_URLS)}")
    key_env = f"{provider_name.upper()}_API_KEY"
    key_val = os.environ.get(key_env)
    if not key_val:
        pytest.skip(f"set {key_env} to run this live test")

    repo = make_repo(tmp_path / "repo", {"README.md": "Initial README\n"}, git_env)
    app_settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})

    async with httpx2.AsyncClient(timeout=120.0) as http:
        github = GitHubAuth(databases, keystore, http, FakeGh(None))
        runner = GitRunner(paths.bin, github.token)
        workspace_svc = WorkspaceService(
            app_settings, paths, databases, runner, github, http, memory_project_secrets(databases)
        )
        events = SqliteEventLog(databases)
        project = await workspace_svc.add_local(str(repo))
        await set_permission_level(databases, project.id, "autonomous")

        tasks_svc = TasksService(
            databases=databases, paths=paths, git=runner, events=events, workspace=workspace_svc
        )

        tools = default_registry(tasks=tasks_svc)
        catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
        failover = KeyFailover()  # a real provider: its rate limits clear in real time
        providers_svc = ProvidersService(databases, keystore, http, failover)
        agent_settings = AgentSettingsService(databases, providers_svc, catalog)

        prov = await providers_svc.create_provider(
            ProviderCreate(name=provider_name, kind="hosted", base_url=base_url)
        )
        await providers_svc.add_key(prov.name, key_val)
        await providers_svc.add_model(ModelCreate(provider=provider_name, wire_model_id=wire_id))
        await providers_svc.verify_model(live_model)

        chat_agents = [d.name for d in catalog.all() if d.chat]
        for agent in chat_agents:
            await agent_settings.set_default_model(agent, live_model)

        status_board = SqliteStatusBoard(catalog, events)
        hooks = status_board_hooks(status_board)
        approval_store = ApprovalStore(databases)

        manager = SessionManager(
            databases=databases,
            workspace=workspace_svc,
            catalog=catalog,
            tools=tools,
            settings=agent_settings,
            providers=providers_svc,
            http=http,
            failover=failover,
            gate=ApprovalGate(databases, store=approval_store),
            inbound=NoInbound(),
            turn_end=NoTurnEndCheck(),
            workspaces=tasks_svc,
            approval_store=approval_store,
            hooks=hooks,
        )

        for agent in chat_agents:
            start = await manager.get(project.id, agent)
            sub = manager.subscribe(project.id, agent, after_seq=start.last_seq)
            prompt = f"Hello {agent}, please reply with a short greeting and introduce your role in one sentence."
            await manager.send(project.id, agent, prompt)

            events_list: list[StreamEvent] = []
            async for ev in sub:
                events_list.append(ev)
                if isinstance(ev, TurnEnded):
                    break

            assert any(isinstance(e, TurnEnded) and e.data.status == "done" for e in events_list)
            view = await manager.get(project.id, agent)
            assert len(view.messages) >= 2
            agent_msg = view.messages[-1]
            text = "".join(p.text for p in agent_msg.parts if p.type == "text")
            print(f"\nLive model response from {agent}:\n{text}\n")
            assert len(text) > 0

        # Tool-using agent called one of its own tools
        start = await manager.get(project.id, "orchestrator")
        sub = manager.subscribe(project.id, "orchestrator", after_seq=start.last_seq)
        await manager.send(project.id, "orchestrator", "What does README.md say? Please read it.")
        async for ev in sub:
            if isinstance(ev, TurnEnded):
                break

        orch_defn = catalog.get("orchestrator")
        orch_view = await manager.get(project.id, "orchestrator")
        has_tool_call = any(
            p.type == "tool_call" and p.name in orch_defn.tools
            for m in orch_view.messages
            for p in m.parts
        )
        assert has_tool_call, "Orchestrator did not call any of its own tools"

        await manager.close()
