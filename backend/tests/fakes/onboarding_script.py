"""The scripted models `fake-onboarding` and `fake-audit` of the fake OpenAI server, and the data
folder their story is about.

Each reply is derived from the conversation alone, never from server state, so tests stay
independent. No script runs Bash or Docker: the e2e CI has no Docker.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final, cast

from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.proposals.contract import CONNECTIONS_KIND, HANDOFF_KIND

DATA_PATH_KEY: Final = "DATA_PATH="
MLFLOW_URL_KEY: Final = "MLFLOW_URL="
CONNECTION_NAME: Final = "hotel-data"
MODEL_NAME: Final = "hotel-cancel"
BOOKINGS: Final = "bookings_e2e"
PREDICTIONS: Final = "predictions_e2e"
LABELS: Final = "labels_e2e"
TABLES: Final = (BOOKINGS, PREDICTIONS, LABELS)
FEED_TIMEZONE: Final = "Europe/Lisbon"
ON_CALL_TIMEZONE: Final = "Europe/Madrid"

ASK_FOR_DATA: Final = (
    f"Where does the data live? Answer with {DATA_PATH_KEY}<folder> and {MLFLOW_URL_KEY}<url>, "
    "each on its own line."
)
NO_PROPOSE: Final = f"I cannot set the project up: the {PROPOSE_TOOL_NAME} tool is not offered."
ONBOARDING_COMPLETE: Final = "Onboarding is complete."
AUDIT_CONTEXT: Final = "Audit context included: "
AUDIT_NO_DATA: Final = "Audit saw no data."
AUDIT_LINE_CAP: Final = 160

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


def _value(text: str, key: str) -> str:
    """The value after `key`, to the end of its line."""
    match = re.search(re.escape(key) + r"(.*)", text)
    return match.group(1).strip() if match else ""


def _propose(kind: str, payload: dict[str, Any]) -> ToolCall:
    # The fake server serializes the arguments with json.dumps, so a path's backslashes survive.
    return {"name": PROPOSE_TOOL_NAME, "arguments": {"kind": kind, "payload": payload}}


def connections_payload(path: str) -> dict[str, Any]:
    return {
        "connections": [
            {
                "kind": LOCAL_FOLDER_KIND,
                "name": CONNECTION_NAME,
                "settings": {"path": path},
                "why": "The hotel's booking, prediction and label exports",
            }
        ]
    }


def _cron(expression: str) -> dict[str, Any]:
    return {"cron": expression, "every_minutes": None}


def _role(table: str, role: str, id_column: str, time_column: str | None) -> dict[str, Any]:
    return {
        "connection": CONNECTION_NAME,
        "table": table,
        "role": role,
        "id_column": id_column,
        "time_column": time_column,
    }


def _model() -> dict[str, Any]:
    return {
        "name": MODEL_NAME,
        "profile": {
            "objective": "Predict which bookings will be cancelled",
            "problem_type": "binary_classification",
            "target_column": "canceled",
            "positive_class": "1",
            "primary_metric": "f1",
            "metric_rationale": "Missed cancellations and false alarms both cost rooms",
            "operating_threshold": 0.5,
            "label_delay_days": 7,
            "label_maturity_days": 14,
            "serving_mode": "batch",
            "error_costs": "A missed cancellation costs three times a false alarm",
            "cost_ratio": 3.0,
            "groups": [{"column": "country", "values": None}],
        },
        "predictions": {"connection": CONNECTION_NAME, "table": PREDICTIONS},
        "labels": {"connection": CONNECTION_NAME, "table": LABELS},
        "commands": [
            {
                "kind": "train",
                "type": "command",
                "template": "python train.py --model {model}",
                "workflow": None,
                "inputs": {},
                "placeholders": ["model"],
                "deploys": False,
                "entry_files": ["train.py"],
            }
        ],
        "promotion": "team_gate",
        "environments": {"production": "production"},
        "production_signal": {"kind": "tracker_alias", "ref": "production"},
    }


def handoff_payload(mlflow_url: str) -> dict[str, Any]:
    """A handoff covering each part of the full payload, about the fixture folder's tables."""
    file_results = {"connection": CONNECTION_NAME, "path_pattern": "results/*.json"}
    return {
        "models": [_model()],
        "commands": [
            {
                "kind": "hold",
                "type": "command",
                "template": "python hold.py --until {window_end}",
                "workflow": None,
                "inputs": {},
                "placeholders": ["window_end"],
                "deploys": False,
                "entry_files": ["hold.py"],
            }
        ],
        "data_roles": [
            _role(BOOKINGS, "training", "booking_id", "arrival_date"),
            _role(PREDICTIONS, "predictions", "booking_id", "scored_at"),
            _role(LABELS, "labels", "booking_id", None),
        ],
        "tracking": [{"type": "mlflow", "settings": {"url": mlflow_url}, "auth": "none"}],
        "pipeline": [
            {
                "name": "prepare",
                "entry": "prepare.py",
                "inputs": ["bookings"],
                "outputs": ["features"],
            },
            {"name": "train", "entry": "train.py", "inputs": ["features"], "outputs": ["model"]},
        ],
        "edges": [{"from_stage": "prepare", "to_stage": "train"}],
        "operations": {
            "scheduler": {"name": "cron", "config_location": "deploy/crontab"},
            "feeds": [
                {
                    "name": "daily-bookings",
                    "kind": "data",
                    "ref": {"connection": CONNECTION_NAME, "table": BOOKINGS},
                    "cadence": _cron("0 6 * * *"),
                    "grace_minutes": 60,
                    "timezone": FEED_TIMEZONE,
                }
            ],
            "on_call": [
                {
                    "name": "ml-team",
                    "contacts": [
                        {"channel": "discord", "address": "#ml-alerts"},
                        {"channel": "email", "address": "ml@hotel.test"},
                    ],
                    "hours": {
                        "days": ["mon", "tue", "wed", "thu", "fri"],
                        "start": "08:30",
                        "end": "17:30",
                    },
                    "timezone": ON_CALL_TIMEZONE,
                    "escalation_order": 1,
                }
            ],
            "checks": [
                {
                    "check_id": "chk-bookings",
                    "name": "Bookings quality",
                    "category": "data_quality",
                    "cadence": _cron("30 6 * * *"),
                    "timezone": "UTC",
                    "results": {"connection": CONNECTION_NAME, "path_pattern": "checks/*.json"},
                }
            ],
            "results_location": file_results,
        },
        "gaps": [{"what": "No drift check", "next_step": "Add a weekly drift check"}],
        "grants": {"network": False, "permission_level": "ask"},
    }


def onboarding_reply(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall] | None]:
    """Asks for the data, then proposes the connection, then the handoff, then finishes.

    The step is the number of tool results after the latest user message giving `DATA_PATH=`,
    so a new such message starts a new round.
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
        return "", [_propose(CONNECTIONS_KIND, connections_payload(_value(request, DATA_PATH_KEY)))]
    if results == 1:
        return "", [_propose(HANDOFF_KIND, handoff_payload(_value(request, MLFLOW_URL_KEY)))]
    return ONBOARDING_COMPLETE, None


def audit_reply(messages: list[dict[str, Any]]) -> str:
    """Quotes the first line of the request naming the bookings table: the system message comes
    first, so a `data_summary` block there is found before any message's mention."""
    for message in messages:
        for line in _text(message.get("content")).splitlines():
            if BOOKINGS in line:
                return AUDIT_CONTEXT + line.strip()[:AUDIT_LINE_CAP]
    return AUDIT_NO_DATA


_ROWS: Final = 7


def write_data_folder(folder: Path) -> None:
    """Three tables of a header and 7 rows each (predictions and labels share their ID column)
    and one unsupported file. The e2e writes the same tables in `frontend/e2e/repo.ts`."""
    folder.mkdir(parents=True)
    ids = [f"B{n:03d}" for n in range(1, _ROWS + 1)]
    countries = ["PRT", "GBR", "FRA", "ESP", "DEU", "PRT", "GBR"]
    bookings = ["booking_id,arrival_date,country,lead_time,canceled"] + [
        f"{b},2026-10-{n + 1:02d},{countries[n]},{10 * (n + 1)},{n % 2}" for n, b in enumerate(ids)
    ]
    predictions = ["booking_id,scored_at,score"] + [
        f"{b},2026-10-{n + 1:02d}T06:00:00,0.{n + 2}" for n, b in enumerate(ids)
    ]
    labels = ["booking_id,canceled"] + [f"{b},{n % 2}" for n, b in enumerate(ids)]
    for table, lines in ((BOOKINGS, bookings), (PREDICTIONS, predictions), (LABELS, labels)):
        (folder / f"{table}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "notes.txt").write_text("Exported every morning at 06:00.\n", encoding="utf-8")
