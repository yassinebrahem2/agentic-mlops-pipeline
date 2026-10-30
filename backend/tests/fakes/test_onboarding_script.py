"""The scripted onboarding and audit models: their payloads are the real contracts', so a contract
change fails here, not in the browser."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME, ProposeArgs
from mlops_agents.connections.contract import LocalFolderSettings
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.onboarding import handoff_check
from mlops_agents.proposals.contract import CONNECTIONS_KIND, HANDOFF_KIND
from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload

# Pyright checks tests/fakes as its own root (pyproject's executionEnvironments).
if TYPE_CHECKING:
    from onboarding_script import (
        AUDIT_NO_DATA,
        CONNECTION_NAME,
        MODEL_NAME,
        ONBOARDING_COMPLETE,
        TABLES,
        audit_reply,
        onboarding_reply,
        write_data_folder,
    )
else:
    from tests.fakes.onboarding_script import (
        AUDIT_NO_DATA,
        CONNECTION_NAME,
        MODEL_NAME,
        ONBOARDING_COMPLETE,
        TABLES,
        audit_reply,
        onboarding_reply,
        write_data_folder,
    )

# A Windows path with spaces: its backslashes must survive the JSON of the tool call.
DATA_PATH = r"C:\Users\Jane Doe\hotel exports"
MLFLOW_URL = "http://127.0.0.1:5999"
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": name, "parameters": {}}}
    for name in ("Read", PROPOSE_TOOL_NAME)
]


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _tool_round(call_id: str, result: str) -> list[dict[str, Any]]:
    call = {"id": call_id, "type": "function", "function": {"name": PROPOSE_TOOL_NAME}}
    return [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def _start() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are Onboarding."},
        _user('Please onboard the project "hotel". Its repository is /repo.'),
        {"role": "assistant", "content": "Where does the data live?"},
        _user(f"DATA_PATH={DATA_PATH}\nMLFLOW_URL={MLFLOW_URL}"),
    ]


def _propose(messages: list[dict[str, Any]]) -> ProposeArgs:
    text, calls = onboarding_reply(messages, TOOLS)
    assert text == ""
    assert calls is not None
    [call] = calls
    assert call["name"] == PROPOSE_TOOL_NAME
    # The fake streams the arguments as JSON: parse them back the way the tool does.
    return ProposeArgs.model_validate(json.loads(json.dumps(call["arguments"])))


def test_before_any_data_path_it_asks_where_the_data_lives() -> None:
    text, calls = onboarding_reply(_start()[:2], TOOLS)

    assert calls is None
    assert "DATA_PATH=" in text


def test_a_data_path_proposes_one_local_folder_connection_at_that_path() -> None:
    args = _propose(_start())

    assert args.kind == CONNECTIONS_KIND
    assert isinstance(args.payload, ConnectionsPayload)
    [spec] = args.payload.connections
    assert spec.name == CONNECTION_NAME
    assert spec.kind == "local_folder"
    assert spec.settings.path == DATA_PATH


def test_after_one_tool_result_it_proposes_a_handoff_the_fixture_folder_resolves(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "exports"
    write_data_folder(folder)
    messages = [*_start(), *_tool_round("call_1", "Approved. Everything was applied.")]

    args = _propose(messages)

    assert args.kind == HANDOFF_KIND
    payload = args.payload
    assert isinstance(payload, HandoffPayload)
    assert [m.name for m in payload.models] == [MODEL_NAME]
    [source] = payload.tracking
    assert (source.type, source.auth) == ("mlflow", "none")
    assert source.settings.model_dump() == {"url": MLFLOW_URL}
    kind = LocalFolderKind(tmp_path / "home")
    tables = [t.name for t in kind.tables(LocalFolderSettings(path=str(folder)))]
    assert sorted(tables) == sorted(TABLES)
    assert handoff_check.check(payload, [CONNECTION_NAME], {CONNECTION_NAME: tables}) == []


def test_the_handoff_covers_every_part() -> None:
    messages = [*_start(), *_tool_round("call_1", "Approved.")]
    payload = _propose(messages).payload
    assert isinstance(payload, HandoffPayload)

    [model] = payload.models
    assert model.profile.primary_metric == "f1"
    assert model.predictions is not None and model.labels is not None
    assert [c.type for c in model.commands] == ["command"]
    assert model.commands[0].placeholders
    assert model.promotion == "team_gate"
    assert model.environments
    assert model.production_signal is not None
    assert model.production_signal.kind == "tracker_alias"
    assert [c.kind for c in payload.commands] == ["hold"]
    assert sorted(r.role for r in payload.data_roles) == ["labels", "predictions", "training"]
    [predictions] = [r for r in payload.data_roles if r.role == "predictions"]
    assert predictions.id_column and predictions.time_column
    assert (len(payload.pipeline), len(payload.edges)) == (2, 1)
    ops = payload.operations
    assert ops.scheduler is not None
    [feed] = ops.feeds
    assert feed.kind == "data" and feed.cadence.cron and feed.timezone
    [on_call] = ops.on_call
    assert on_call.hours.days and len(on_call.contacts) == 2
    [check] = ops.checks
    assert check.results.model_dump().keys() == {"connection", "path_pattern"}
    assert len(payload.gaps) == 1
    assert (payload.grants.network, payload.grants.permission_level) == (False, "ask")


def test_after_two_tool_results_it_finishes() -> None:
    messages = [
        *_start(),
        *_tool_round("call_1", "Approved."),
        *_tool_round("call_2", "Approved."),
    ]

    assert onboarding_reply(messages, TOOLS) == (ONBOARDING_COMPLETE, None)


def test_a_new_data_path_message_starts_a_new_round() -> None:
    messages = [
        *_start(),
        *_tool_round("call_1", "Approved."),
        *_tool_round("call_2", "Approved."),
        {"role": "assistant", "content": ONBOARDING_COMPLETE},
        _user(f"DATA_PATH={DATA_PATH}\nMLFLOW_URL={MLFLOW_URL}"),
    ]

    assert _propose(messages).kind == CONNECTIONS_KIND
    messages += _tool_round("call_3", "Approved.")
    assert _propose(messages).kind == HANDOFF_KIND


def test_a_rejected_connections_proposal_still_counts_as_a_step() -> None:
    messages = [*_start(), *_tool_round("call_1", "Rejected by the user.")]

    assert _propose(messages).kind == HANDOFF_KIND


def test_without_the_propose_tool_it_only_answers() -> None:
    text, calls = onboarding_reply(_start(), TOOLS[:1])

    assert calls is None
    assert PROPOSE_TOOL_NAME in text


def test_the_text_parts_of_a_list_content_are_read() -> None:
    messages = [
        _user("hello"),
        {"role": "user", "content": [{"type": "text", "text": "DATA_PATH=/d"}]},
    ]

    args = _propose(messages)

    assert isinstance(args.payload, ConnectionsPayload)
    assert args.payload.connections[0].settings.path == "/d"


@pytest.mark.parametrize(
    "content",
    [
        'Data connections:\nTable "bookings_e2e" (csv): 7 rows\nColumns:',
        [{"type": "text", "text": 'Data connections:\nTable "bookings_e2e" (csv): 7 rows'}],
    ],
)
def test_audit_quotes_the_first_line_naming_the_bookings_table(content: Any) -> None:
    messages = [{"role": "system", "content": content}, _user("bookings_e2e is late")]

    assert audit_reply(messages) == 'Audit context included: Table "bookings_e2e" (csv): 7 rows'


def test_audit_cuts_the_line_at_160_characters() -> None:
    line = "bookings_e2e " + "x" * 300

    reply = audit_reply([{"role": "system", "content": line}])

    assert reply == f"Audit context included: {line[:160]}"


def test_audit_without_the_table_says_so() -> None:
    assert audit_reply([{"role": "system", "content": "No data."}, _user("Hi")]) == AUDIT_NO_DATA


def test_the_fixture_folder_holds_three_tables_and_one_unsupported_file(tmp_path: Path) -> None:
    folder = tmp_path / "exports"
    write_data_folder(folder)
    kind = LocalFolderKind(tmp_path / "home")
    settings = LocalFolderSettings(path=str(folder))

    findings = kind.findings(settings)

    assert [f.path for f in findings.unsupported_files] == ["notes.txt"]
    for name in TABLES:
        lines = (folder / f"{name}.csv").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 8  # a header and 7 rows
