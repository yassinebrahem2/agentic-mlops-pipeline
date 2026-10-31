"""The scripted models for monitoring agents: alert-prioritization, data-quality, deployment,
and onboarding-night.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, cast

from mlops_agents.agents.tools.names import (
    INCIDENT_UPDATE,
    MONITORING_QUERY,
    RUN_HOOK,
)
from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME

if TYPE_CHECKING:
    from onboarding_script import connections_payload

    from tests.e2e.fixture_project import (
        ROLLBACK_HOOK_ID,
        ROLLBACK_RULE_ID,
        playwright_handoff_payload,
    )
else:
    from tests.e2e.fixture_project import (
        ROLLBACK_HOOK_ID,
        ROLLBACK_RULE_ID,
        playwright_handoff_payload,
    )
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

ToolCall = dict[str, Any]


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
    args: dict[str, Any] = {
        "hook_id": ROLLBACK_HOOK_ID,
        "reason": "Scoring failure detected; executing automated rollback",
        "rule_id": ROLLBACK_RULE_ID,
    }
    if inc_id is not None:
        args["incident_id"] = inc_id

    return "", [{"name": RUN_HOOK, "arguments": args}]


def onboarding_night_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Scripted onboarding model for the e2e night fixture (Playwright variant).
    Asks for data, proposes connections, proposes the night/playwright handoff, and finishes.
    """
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
                    "payload": playwright_handoff_payload(mlflow_url),
                },
            }
        ]
    return ONBOARDING_COMPLETE, None
