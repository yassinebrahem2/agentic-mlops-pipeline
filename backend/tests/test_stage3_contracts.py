"""The stage 3 contract modules import cleanly (no cycles) and expose the I13 to I18 names."""

import importlib
import inspect
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, ClassVar, get_args, get_type_hints
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, Field, ValidationError

from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.storage.migrate import migrate

MODULES = [
    "mlops_agents.proposals.payloads",
    "mlops_agents.proposals.contract",
    "mlops_agents.connections.contract",
    "mlops_agents.tracking.contract",
    "mlops_agents.knowledge.contract",
    "mlops_agents.agents.hooks.contract",
    "mlops_agents.providers.embedder",
    "mlops_agents.agents.runtime.contract",
    "mlops_agents.events.contract",
]


@pytest.mark.parametrize("module", MODULES)
def test_each_contract_module_imports(module: str) -> None:
    importlib.import_module(module)


def _methods(protocol: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(protocol) if not name.startswith("_")}


def _attributes(protocol: type) -> set[str]:
    return set(get_type_hints(protocol))


def test_connection_protocols() -> None:
    from mlops_agents.connections.contract import ConnectionKind, Connections

    assert _methods(ConnectionKind) == {
        "validate",
        "findings",
        "tables",
        "data_version",
        "duckdb_source",
        "sandbox_mount",
    }
    assert _attributes(ConnectionKind) == {"name", "settings_model", "needs_credential"}
    assert _methods(Connections) == {
        "list",
        "inspect",
        "create",
        "update",
        "delete",
        "tables",
        "data_version",
        "duckdb_source",
        "mounts",
    }


def test_tracking_protocols() -> None:
    from mlops_agents.tracking.contract import Tracking, TrackingAdapter

    assert _methods(Tracking) == {"sources", "create_source", "sync", "query"}
    assert _methods(TrackingAdapter) == {"sync"}
    assert _attributes(TrackingAdapter) == {"type"}


def test_knowledge_protocols() -> None:
    from mlops_agents.knowledge.contract import Knowledge, VectorStore

    assert _methods(Knowledge) == {"save", "get", "search", "delete"}
    assert _methods(VectorStore) == {"upsert", "remove", "nearest", "drop_embedder"}


def test_embedder_protocols() -> None:
    from mlops_agents.providers.embedder import Embedder, Embedders, ModelType

    assert _methods(Embedders) == {"for_project", "status"}
    assert _methods(Embedder) == {"embed"}
    assert _attributes(Embedder) == {"address", "dimensions"}
    assert get_args(ModelType) == ("chat", "embedder")


def test_context_hook_protocol() -> None:
    from mlops_agents.agents.hooks.contract import ContextHook, HookContext

    assert _methods(ContextHook) == {"render"}
    assert _attributes(ContextHook) == {"name"}
    assert set(HookContext.model_fields) == {"project_id", "agent", "trigger_text"}


def test_local_folder_kind_name_has_one_source() -> None:
    from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, LocalFolderKindName

    assert LOCAL_FOLDER_KIND == "local_folder"
    assert get_args(LocalFolderKindName) == (LOCAL_FOLDER_KIND,)


@pytest.mark.parametrize("name", ["hotel-data", "a1"])
def test_connection_name_pattern_accepts(name: str) -> None:
    from mlops_agents.connections.contract import CONNECTION_NAME_PATTERN

    assert re.fullmatch(CONNECTION_NAME_PATTERN, name)


@pytest.mark.parametrize("name", ["hotel data", "Hotel", "-hotel", "a" * 41])
def test_connection_name_pattern_rejects(name: str) -> None:
    from mlops_agents.connections.contract import CONNECTION_NAME_PATTERN

    assert re.fullmatch(CONNECTION_NAME_PATTERN, name) is None


def test_settings_models_reject_unknown_fields() -> None:
    from mlops_agents.connections.contract import LocalFolderSettings
    from mlops_agents.tracking.contract import GithubActionsSettings, MlflowSettings

    with pytest.raises(ValidationError):
        LocalFolderSettings.model_validate({"path": "/data", "token": "x"})
    with pytest.raises(ValidationError):
        MlflowSettings.model_validate({"url": "http://mlflow:5000", "token": "x"})
    with pytest.raises(ValidationError):
        GithubActionsSettings.model_validate({"repository": "a/b", "token": "x"})
    assert GithubActionsSettings(repository="a/b").workflows == []


def test_mlflow_url_with_userinfo_is_rejected() -> None:
    from mlops_agents.tracking.contract import MlflowSettings

    with pytest.raises(ValidationError) as err:
        MlflowSettings(url="http://user:pw@host")
    assert "pw" not in err.value.errors()[0]["msg"]
    assert MlflowSettings(url="http://mlflow:5000").url == "http://mlflow:5000"


class _Inner(BaseModel):
    count: int = Field(ge=1, le=9)
    label: str = Field(max_length=5)


class _Outer(BaseModel):
    inner: _Inner


class _NestedArgs(BaseModel):
    one: _Inner
    many: list[_Inner]
    maybe: _Inner | None = None
    deep: _Outer


class _NestedTool(AgentTool[_NestedArgs]):
    name: ClassVar[str] = "Nested"
    description: ClassVar[str] = "A tool whose arguments nest models."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = _NestedArgs

    async def run(self, ctx: ToolContext, args: _NestedArgs) -> ToolOutput:
        return ToolOutput("ok")


def test_tool_schema_inlines_nested_models() -> None:
    parameters: dict[str, Any] = _NestedTool().schema()["function"]["parameters"]
    text = json.dumps(parameters)
    assert "$ref" not in text
    assert "$defs" not in text
    inner = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 9},
            "label": {"type": "string", "maxLength": 5},
        },
        "required": ["count", "label"],
    }
    props = parameters["properties"]
    assert props["one"] == inner
    assert props["many"] == {"type": "array", "items": inner}
    assert props["maybe"] == {**inner, "default": None}
    assert props["deep"]["properties"]["inner"] == inner


def test_error_kind_defaults_to_null() -> None:
    from mlops_agents.agents.runtime.contract import ErrorKind, MessageView, TurnEndedData

    assert get_args(ErrorKind) == ("keys_exhausted",)
    ended = TurnEndedData(turn_id="trn_1", status="failed", error="boom")
    assert ended.error_kind is None
    message = MessageView.model_validate(
        {
            "id": "msg_1",
            "turn_id": None,
            "author_kind": "agent",
            "author_name": "coder",
            "from_agent": None,
            "parts": [],
            "status": "complete",
            "created_at": "2026-10-28T00:00:00Z",
        }
    )
    assert message.error_kind is None


def test_provider_cooldown_default_matches_the_column(tmp_path: Path) -> None:
    from mlops_agents.providers.models import DEFAULT_COOLDOWN_MINUTES, Provider

    provider = Provider(
        id="prv_1",
        name="groq",
        kind="hosted",
        base_url="https://api.groq.com/openai/v1",
        status="ok",
        status_detail=None,
        keys=[],
    )
    assert provider.cooldown_minutes == DEFAULT_COOLDOWN_MINUTES == 15
    db = tmp_path / "app.db"
    migrate(db, "app", tmp_path / "bk")
    conn = sqlite3.connect(db)
    try:
        defaults = {str(r[1]): r[4] for r in conn.execute("PRAGMA table_info(providers)")}
    finally:
        conn.close()
    assert defaults["cooldown_minutes"] == str(DEFAULT_COOLDOWN_MINUTES)


def test_stage_3_event_types_are_registered() -> None:
    from mlops_agents.events.contract import EventType

    assert {
        "connection.created",
        "connection.updated",
        "connection.deleted",
        "tracking.synced",
        "proposal.created",
        "proposal.decided",
        "handoff.approved",
        "turn.stalled",
        "turn.resumed",
        "note.saved",
        "note.deleted",
    } <= set(get_args(EventType))


def test_duckdb_version_and_sqlite_vec() -> None:
    import duckdb
    import sqlite_vec  # pyright: ignore[reportMissingTypeStubs]  # sqlite-vec ships no type stubs

    from mlops_agents.config import DUCKDB_VERSION

    assert duckdb.__version__ == DUCKDB_VERSION
    assert callable(sqlite_vec.load)


def test_proposal_protocols() -> None:
    from mlops_agents.proposals.contract import (
        CONNECTIONS_KIND,
        HANDOFF_KIND,
        Handoffs,
        ProposalKind,
        Proposals,
    )

    assert _methods(ProposalKind) == {"key", "preview", "warnings", "credentials_needed", "apply"}
    assert _attributes(ProposalKind) == {"name", "payload_model"}
    assert _methods(Proposals) == {
        "create",
        "attach",
        "get_by_approval",
        "pending",
        "latest_for_turn",
    }
    assert _methods(Handoffs) == {"current", "store"}
    assert (CONNECTIONS_KIND, HANDOFF_KIND) == ("connections", "handoff")


def test_proposal_models_have_the_overview_fields() -> None:
    from mlops_agents.proposals.contract import (
        ApplyResult,
        CredentialSlot,
        HandoffView,
        ProposalStatus,
        ProposalView,
        SlotState,
    )

    assert get_args(ProposalStatus) == ("pending", "approved", "rejected", "superseded")
    assert list(CredentialSlot.model_fields) == ["index", "label", "source_type"]
    assert list(SlotState.model_fields) == ["index", "label", "set"]
    assert list(ApplyResult.model_fields) == ["applied", "failed"]
    assert list(ProposalView.model_fields) == [
        "id",
        "kind",
        "key",
        "payload",
        "warnings",
        "status",
        "agent",
        "approval_id",
        "turn_id",
        "tool_call_id",
        "result",
        "slots",
        "created",
    ]
    assert list(HandoffView.model_fields) == ["proposal_id", "version", "approved", "payload"]


def test_tzdata_supplies_the_zone_database() -> None:
    import tzdata  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert ZoneInfo("Europe/Paris").key == "Europe/Paris"
