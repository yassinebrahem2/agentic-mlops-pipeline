"""The scripted models for monitoring agents: alert-prioritization, data-quality, deployment,
and onboarding-night.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from mlops_agents.agents.tools.names import (
    INCIDENT_UPDATE,
    MONITORING_QUERY,
    RECORD_FIX_CHOICE,
    REQUEST_FIX,
    RUN_HOOK,
    SAVE_REPORT,
)
from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME
from mlops_agents.promotion.constants import REQUIRED_REPORT_FIELDS

if TYPE_CHECKING:
    from onboarding_script import connections_payload

    from tests.e2e.fixture_project import (
        MODEL_NAME,
        ROLLBACK_HOOK_ID,
        ROLLBACK_RULE_ID,
        playwright_handoff_payload,
        promotion_handoff_payload,
    )
    from tests.e2e.seeded_mlflow import CHALLENGER_VERSION, CHAMPION_VERSION
else:
    from tests.e2e.fixture_project import (
        MODEL_NAME,
        ROLLBACK_HOOK_ID,
        ROLLBACK_RULE_ID,
        playwright_handoff_payload,
        promotion_handoff_payload,
    )
    from tests.e2e.seeded_mlflow import CHALLENGER_VERSION, CHAMPION_VERSION
    from tests.fakes.onboarding_script import connections_payload

SCRIPTED_DATA_QUALITY_CAUSE: Final = (
    "Data validation check failed: missing required booking values in batch"
)

DATA_PATH_KEY: Final = "DATA_PATH="
MLFLOW_URL_KEY: Final = "MLFLOW_URL="
ASK_FOR_DATA: Final = (
    f"Where does the data live? Answer with {DATA_PATH_KEY}<folder> and {MLFLOW_URL_KEY}<url>, "
    "each on its own line."
)
NO_PROPOSE: Final = f"I cannot set the project up: the {PROPOSE_TOOL_NAME} tool is not offered."
ONBOARDING_COMPLETE: Final = "Onboarding is complete."

_deployment_hook_id: str | None = None
_deployment_rule_id: str | None = None


def set_deployment_ids(*, hook_id: str | None = None, rule_id: str | None = None) -> None:
    global _deployment_hook_id, _deployment_rule_id
    _deployment_hook_id = hook_id
    _deployment_rule_id = rule_id


def reset_deployment_ids() -> None:
    global _deployment_hook_id, _deployment_rule_id
    _deployment_hook_id = None
    _deployment_rule_id = None


ToolCall = dict[str, Any]

# The fix story's scripted models (model-performance, retraining, deployment). Like the
# deployment ids above, the test sets what they cite; they never read server state.
FIX_WAKE: Final = "Fix request "
LINKED_WAKE: Final = "Training run linked for fix "
POST_DEPLOY_WAKE: Final = "Deployment of model "
EVALUATION_PR_MESSAGE: Final = "Improve the evaluation step: add expected_cost."
EVALUATION_PR_TITLE: Final = "add expected_cost to the evaluation record"
POST_DEPLOY_FOLLOW_UP: Final = "Compare the live label results with the evaluation record."
FIX_EVIDENCE: Final = "The label-based check mp-label-check fired: f1 0.52 on 480 labels."
PROMOTION_KIND_NAME: Final = "promotion"
PROMOTE_REQUEST: Final = "Propose the promotion of the newest candidate."
START_TASK: Final = "StartTask"
WRITE: Final = "Write"
READ: Final = "Read"
OPEN_PULL_REQUEST: Final = "OpenPullRequest"
SCHEDULE_WAKEUP: Final = "ScheduleWakeup"
TRACKING_HISTORY: Final = "TrackingHistory"
EVALUATION_FILE: Final = "evaluate.py"
EVALUATION_FIELD: Final = "expected_cost"
TEAM_GATE_TITLE: Final = "Team gate"  # the title the app's team gate message starts with
NEW_VERSION_LINE: Final = "New version: "  # a line of the app's post_deploy wake text
FIRST_ON_CALL: Final = "alice"  # the fixture's first on-call person, as the log stores it


@dataclass
class FixStoryIds:
    """What the retraining and deployment scripts cite in the fix story."""

    model: str = ""
    train_hook_id: str = ""
    deploy_hook_id: str | None = None
    window_start: str = ""
    window_end: str = ""
    candidate_version: str = ""
    champion_version: str | None = None


_fix_story = FixStoryIds()


def set_fix_story_ids(ids: FixStoryIds) -> None:
    global _fix_story
    _fix_story = ids


def reset_fix_story_ids() -> None:
    set_fix_story_ids(FixStoryIds())


def _text(content: object) -> str:
    """A message's text: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            cast(dict[str, object], p) for p in cast(list[object], content) if isinstance(p, dict)
        ]
        return "\n".join(text for p in parts if isinstance(text := p.get("text"), str))
    return ""


def _extract_incident_id(text: str) -> str | None:
    match = re.search(r"Incident\s+([a-zA-Z0-9_-]+)", text)
    if match:
        return match.group(1)
    match2 = re.search(r"incident_id[=:\s]+([a-zA-Z0-9_-]+)", text)
    if match2:
        return match2.group(1)
    return None


def _find_incident_id(messages: list[dict[str, Any]]) -> str | None:
    for m in reversed(messages):
        txt = _text(m.get("content"))
        inc_id = _extract_incident_id(txt)
        if inc_id:
            return inc_id
    return None


def _value(text: str, key: str) -> str:
    match = re.search(re.escape(key) + r"(.*)", text)
    return match.group(1).strip() if match else ""


def alert_prioritization_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Alert Prioritization agent:
    Holds [Read, Glob, Grep, MonitoringQuery, IncidentMerge, IncidentUpdate, AdjustSeverity].
    Woken on new/changed incident, calls MonitoringQuery, then concludes.
    """
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return "Alert prioritization review complete.", None

    inc_id = _find_incident_id(messages)
    query_args: dict[str, Any] = {"what": "incidents", "limit": 10}
    if inc_id is not None:
        query_args["id"] = inc_id

    return "", [{"name": MONITORING_QUERY, "arguments": query_args}]


def data_quality_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Data Quality agent:
    Woken on incident, diagnoses and calls IncidentUpdate with the diagnosed cause.
    """
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return "Diagnosis complete: incident cause recorded.", None

    inc_id = _find_incident_id(messages)
    if inc_id is not None:
        return "", [
            {
                "name": INCIDENT_UPDATE,
                "arguments": {
                    "incident_id": inc_id,
                    "cause": SCRIPTED_DATA_QUALITY_CAUSE,
                },
            }
        ]

    return "Diagnosis complete: no active incident found.", None


def deployment_rollback_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Deployment & Rollback agent:
    Woken on critical scoring failure, calls RunHook citing the rollback rule.
    """
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return "Rollback executed via rule.", None

    inc_id = _find_incident_id(messages)
    hook_id = _deployment_hook_id or ROLLBACK_HOOK_ID
    rule_id = _deployment_rule_id or ROLLBACK_RULE_ID
    args: dict[str, Any] = {
        "hook_id": hook_id,
        "reason": "Scoring failure detected; executing automated rollback",
        "rule_id": rule_id,
    }
    if inc_id is not None:
        args["incident_id"] = inc_id

    return "", [{"name": RUN_HOOK, "arguments": args}]


def onboarding_night_reply(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    with_deploy_hook: bool = False,
) -> tuple[str, list[ToolCall] | None]:
    """Scripted onboarding model for the e2e night fixture (Playwright variant).
    Asks for data, proposes connections, proposes the night/playwright handoff, and finishes.
    With `with_deploy_hook` the handoff also declares the deploy hook a promotion needs.
    """
    handoff = promotion_handoff_payload if with_deploy_hook else playwright_handoff_payload
    asked = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and DATA_PATH_KEY in _text(m.get("content"))
    ]
    if not asked:
        return ASK_FOR_DATA, None
    if not any(tool["function"]["name"] == PROPOSE_TOOL_NAME for tool in tools):
        return NO_PROPOSE, None
    start = asked[-1]
    request = _text(messages[start].get("content"))
    results = sum(1 for m in messages[start + 1 :] if m.get("role") == "tool")
    if results == 0:
        return "", [
            {
                "name": PROPOSE_TOOL_NAME,
                "arguments": {
                    "kind": "connections",
                    "payload": connections_payload(_value(request, DATA_PATH_KEY)),
                },
            }
        ]
    if results == 1:
        mlflow_url = _value(request, MLFLOW_URL_KEY) or "http://127.0.0.1:5000"
        return "", [
            {
                "name": PROPOSE_TOOL_NAME,
                "arguments": {
                    "kind": "handoff",
                    "payload": handoff(mlflow_url),
                },
            }
        ]
    return ONBOARDING_COMPLETE, None


def onboarding_promotion_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """The night's onboarding, with a deploy hook in the handoff."""
    return onboarding_night_reply(messages, tools, with_deploy_hook=True)


def _last_user_text(messages: list[dict[str, Any]], markers: tuple[str, ...]) -> tuple[str, str]:
    """The newest user message that holds one of the markers: (marker, text), or ("", "")."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        text = _text(m.get("content"))
        for marker in markers:
            if marker in text:
                return marker, text
    return "", ""


def _tool_results_after(messages: list[dict[str, Any]], text: str) -> list[str]:
    """The tool results since the newest user message holding `text`, oldest first."""
    start = 0
    for i, m in enumerate(messages):
        if m.get("role") == "user" and text in _text(m.get("content")):
            start = i
    return [_text(m.get("content")) for m in messages[start + 1 :] if m.get("role") == "tool"]


def _first_id(prefix: str, text: str) -> str | None:
    match = re.search(rf"\b({prefix}_[A-Za-z0-9]+)", text)
    return match.group(1) if match else None


def model_performance_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Model Performance: woken on a model performance incident its label-based check
    confirmed, it requests a fix with the evidence and concludes."""
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return "Fix requested: Retraining chooses and runs it.", None
    inc_id = _find_incident_id(messages)
    if inc_id is None:
        return "No active incident found.", None
    return "", [
        {"name": REQUEST_FIX, "arguments": {"incident_id": inc_id, "evidence": FIX_EVIDENCE}}
    ]


def _report_assessments() -> dict[str, dict[str, str]]:
    return {
        name: {"value": f"Assessed {name.replace('_', ' ')}."} for name in REQUIRED_REPORT_FIELDS
    }


def _deploy_hook_id(hooks_result: str) -> str | None:
    """The ID of the deploy hook in a `MonitoringQuery` result for hooks."""
    start, end = hooks_result.find("["), hooks_result.rfind("]")
    if start < 0 or end < start:
        return None
    rows = cast(list[dict[str, Any]], json.loads(hooks_result[start : end + 1]))
    return next((str(r["id"]) for r in rows if r["spec"]["kind"] == "deploy"), None)


def _promote_request_reply(results: list[str]) -> tuple[str, list[ToolCall] | None]:
    """A person's request to promote the seeded challenger (the e2e server's fake MLflow): find
    the deploy hook, save the report, propose the promotion. The versions and the model are the
    seeded ones; nothing here reads server state beyond the two tool results."""
    if len(results) == 0:
        return "", [{"name": MONITORING_QUERY, "arguments": {"what": "hooks", "model": MODEL_NAME}}]
    if len(results) == 1:
        return "", [
            {
                "name": SAVE_REPORT,
                "arguments": {
                    "draft": {
                        "model": MODEL_NAME,
                        "candidate_version": CHALLENGER_VERSION,
                        "assessments": _report_assessments(),
                    }
                },
            }
        ]
    hook_id = _deploy_hook_id(results[0])
    report_id = _first_id("rep", results[1])
    if len(results) == 2 and hook_id is not None and report_id is not None:
        return "", [
            {
                "name": PROPOSE_TOOL_NAME,
                "arguments": {
                    "kind": PROMOTION_KIND_NAME,
                    "payload": {
                        "model": MODEL_NAME,
                        "report_id": report_id,
                        "candidate_version": CHALLENGER_VERSION,
                        "champion_version": CHAMPION_VERSION,
                        "deploy_hook_id": hook_id,
                    },
                },
            }
        ]
    return "The promotion is proposed.", None


def retraining_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Retraining, in the order of its definition.

    A `fix_request` wake: record `retrain` (and why not each other option), then `RunHook` the
    train hook with the training window; the run asks a person first. The wake that follows the
    run linking back: read the evaluation, save the report, and (in `person` mode) propose the
    promotion with the report's ID from the tool result.
    """
    ids = _fix_story
    marker, text = _last_user_text(messages, (FIX_WAKE, LINKED_WAKE, PROMOTE_REQUEST))
    if not marker:
        return "Nothing to do.", None
    results = _tool_results_after(messages, text)
    if marker == PROMOTE_REQUEST:
        return _promote_request_reply(results)
    fix_id = _first_id("fix", text)
    if marker == FIX_WAKE:
        if len(results) == 0 and fix_id is not None:
            return "", [
                {
                    "name": RECORD_FIX_CHOICE,
                    "arguments": {
                        "fix_id": fix_id,
                        "choice": "retrain",
                        "why": "The relationship changed: the labels confirm the decay.",
                        "why_not": {
                            "re_threshold": "The ranking itself degraded, not only the threshold.",
                            "recalibrate": "The ranking is off, so recalibrating would not help.",
                            "decline": "The labels confirm a real decay.",
                        },
                    },
                }
            ]
        if len(results) == 1:
            return "", [
                {
                    "name": RUN_HOOK,
                    "arguments": {
                        "hook_id": ids.train_hook_id,
                        "reason": "Retrain on the labelled window",
                        "fix_id": fix_id,
                        "params": {
                            "window_start": ids.window_start,
                            "window_end": ids.window_end,
                        },
                    },
                }
            ]
        return "The training run is dispatched; I will read it when it links back.", None
    if len(results) == 0:
        return "", [
            {
                "name": TRACKING_HISTORY,
                "arguments": {"what": "evaluations", "model": ids.model},
            }
        ]
    if len(results) == 1:
        return "", [
            {
                "name": SAVE_REPORT,
                "arguments": {
                    "draft": {
                        "model": ids.model,
                        "candidate_version": ids.candidate_version,
                        "fix_id": fix_id,
                        "assessments": _report_assessments(),
                    }
                },
            }
        ]
    report_id = _first_id("rep", results[1])
    if len(results) == 2 and ids.deploy_hook_id is not None and report_id is not None:
        return "", [
            {
                "name": PROPOSE_TOOL_NAME,
                "arguments": {
                    "kind": PROMOTION_KIND_NAME,
                    "payload": {
                        "model": ids.model,
                        "report_id": report_id,
                        "candidate_version": ids.candidate_version,
                        "champion_version": ids.champion_version,
                        "deploy_hook_id": ids.deploy_hook_id,
                    },
                },
            }
        ]
    return "The report is saved.", None


def deployment_promotion_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Deployment & Rollback in the fix story.

    A `post_deploy` wake: schedule a wake-up to look again. A message from the test asking for
    a better evaluation step: start a task, read and write the file, open the pull request.
    """
    marker, text = _last_user_text(messages, (POST_DEPLOY_WAKE, EVALUATION_PR_MESSAGE))
    if not marker:
        return "Nothing to do.", None
    results = _tool_results_after(messages, text)
    if marker == POST_DEPLOY_WAKE:
        if len(results) == 0:
            return "", [
                {
                    "name": SCHEDULE_WAKEUP,
                    "arguments": {"delay_seconds": 3600, "note": POST_DEPLOY_FOLLOW_UP},
                }
            ]
        return "I will look at the new version again in an hour.", None
    if len(results) == 0:
        return "", [{"name": START_TASK, "arguments": {"title": EVALUATION_PR_TITLE}}]
    if len(results) == 1:
        # An existing file is read before it is written.
        return "", [{"name": READ, "arguments": {"file_path": EVALUATION_FILE}}]
    if len(results) == 2:
        return "", [
            {
                "name": WRITE,
                "arguments": {
                    "file_path": EVALUATION_FILE,
                    "content": f"print('evaluating with {EVALUATION_FIELD}')\n",
                },
            }
        ]
    if len(results) == 3:
        return "", [
            {
                "name": OPEN_PULL_REQUEST,
                "arguments": {
                    "title": EVALUATION_PR_TITLE,
                    "body": "The evaluation record gains expected_cost.",
                },
            }
        ]
    return "The pull request is open.", None
