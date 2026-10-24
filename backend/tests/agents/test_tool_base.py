from pathlib import Path
from typing import ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
    ToolRegistry,
)


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    text: str = Field(description="What to echo.")
    loud: bool = Field(default=False, alias="-l")


class EchoTool(AgentTool[EchoArgs]):
    name: ClassVar[str] = "Echo"
    description: ClassVar[str] = "Echo text back."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = EchoArgs

    async def run(self, ctx: ToolContext, args: EchoArgs) -> ToolOutput:
        if args.text == "fail":
            raise ToolFailure("asked to fail")
        return ToolOutput(args.text.upper() if args.loud else args.text)


class NoFiles:
    @property
    def root(self) -> Path:
        return Path()

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        raise ToolFailure("no files")

    async def resolve_dir(self, user_path: str | None) -> str:
        return ""


CTX = ToolContext(NoFiles())


async def test_invoke_parses_aliases() -> None:
    assert await EchoTool().invoke(CTX, {"text": "hi", "-l": True}) == ToolOutput("HI")


async def test_invalid_arguments_become_an_error_result() -> None:
    out = await EchoTool().invoke(CTX, {"txt": "hi"})
    assert out.is_error
    assert out.content.startswith("Invalid arguments for Echo")


async def test_tool_failure_becomes_an_error_result() -> None:
    assert await EchoTool().invoke(CTX, {"text": "fail"}) == ToolOutput(
        "asked to fail", is_error=True
    )


def test_schema_uses_aliases() -> None:
    schema = EchoTool().schema()
    assert schema["function"]["name"] == "Echo"
    assert set(schema["function"]["parameters"]["properties"]) == {"text", "-l"}


def test_registry() -> None:
    registry = ToolRegistry([EchoTool()])
    assert registry.names() == frozenset({"Echo"})
    assert registry.get("Echo") is not None
    assert registry.get("Nope") is None
    assert [s["function"]["name"] for s in registry.schemas(["Echo"])] == ["Echo"]
    with pytest.raises(ValueError, match="registered twice"):
        registry.register(EchoTool())


class DummyOptionalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    pattern: str = Field(description="Search pattern.")
    path: str | None = Field(default=None, description="Optional path.")
    count: int | None = Field(default=None, ge=0, le=10, description="Optional count.")


class DummyOptionalTool(AgentTool[DummyOptionalArgs]):
    name: ClassVar[str] = "Dummy"
    description: ClassVar[str] = "Dummy tool."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = DummyOptionalArgs

    async def run(self, ctx: ToolContext, args: DummyOptionalArgs) -> ToolOutput:
        return ToolOutput("ok")


def _has_key(obj: object, target: str) -> bool:
    if isinstance(obj, dict):
        d = cast(dict[object, object], obj)
        return target in d or any(_has_key(v, target) for v in d.values())
    if isinstance(obj, list):
        lst = cast(list[object], obj)
        return any(_has_key(item, target) for item in lst)
    return False


def test_schema_strips_title_and_unwraps_nullable_any_of() -> None:
    schema = DummyOptionalTool().schema()
    assert not _has_key(schema, "title")
    assert not _has_key(schema, "anyOf")
    params = schema["function"]["parameters"]
    assert params["required"] == ["pattern"]
    assert params["properties"]["path"]["type"] == "string"
    assert params["properties"]["path"]["default"] is None
    assert params["properties"]["count"]["type"] == "integer"
    assert params["properties"]["count"]["minimum"] == 0
    assert params["properties"]["count"]["maximum"] == 10
    assert params["properties"]["count"]["default"] is None


class ComplexSchemaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="A property literally named title.")
    req_null: str | None = Field(description="Required nullable field.")
    def_five: int | None = Field(default=5, description="Nullable with non-null default.")
    opt: str | None = Field(default=None, description="Nullable with null default.")


class ComplexSchemaTool(AgentTool[ComplexSchemaArgs]):
    name: ClassVar[str] = "Complex"
    description: ClassVar[str] = "Complex schema tool."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = ComplexSchemaArgs

    async def run(self, ctx: ToolContext, args: ComplexSchemaArgs) -> ToolOutput:
        return ToolOutput("ok")


def test_schema_preserves_title_property_and_only_collapses_null_defaults() -> None:
    schema = ComplexSchemaTool().schema()
    params = schema["function"]["parameters"]
    assert "title" not in schema  # schema keyword title removed
    assert "title" in params["properties"]  # property named title preserved
    assert params["properties"]["title"]["type"] == "string"
    assert "title" not in params["properties"]["title"]  # inner title keyword removed

    # Required nullable keeps anyOf
    assert "anyOf" in params["properties"]["req_null"]
    assert set(params["required"]) == {"title", "req_null"}

    # Non-null default keeps anyOf
    assert "anyOf" in params["properties"]["def_five"]
    assert params["properties"]["def_five"]["default"] == 5

    # Null default collapses anyOf into single type
    assert "anyOf" not in params["properties"]["opt"]
    assert params["properties"]["opt"]["type"] == "string"
    assert params["properties"]["opt"]["default"] is None


def test_default_registry_tools_emit_schemas_validating_existing_test_arguments() -> None:
    from mlops_agents.agents.tools import default_registry

    test_cases: dict[str, list[dict[str, object]]] = {
        "Read": [
            {"file_path": "src/app.py"},
            {"file_path": "src/app.py", "offset": 1, "limit": 100},
        ],
        "Glob": [
            {"pattern": "**/*.py"},
            {"pattern": "*.md", "path": "docs"},
        ],
        "Grep": [
            {"pattern": "def train"},
            {"pattern": "def train", "output_mode": "content", "-A": 1},
            {"pattern": "TRAIN", "-i": True, "output_mode": "count", "type": "py"},
            {"pattern": "train", "glob": "*.md"},
            {"pattern": "line", "output_mode": "content", "head_limit": 5, "path": "big.txt"},
        ],
    }

    registry = default_registry()
    for name, payloads in test_cases.items():
        tool = registry.get(name)
        assert tool is not None
        schema = tool.schema()
        assert not _has_key(schema, "title")
        assert not _has_key(schema, "anyOf")
        params = schema["function"]["parameters"]
        req = set(params.get("required", []))
        props = params.get("properties", {})
        for payload in payloads:
            assert req.issubset(payload.keys())
            for key in payload:
                assert key in props
            tool.args_model.model_validate(payload)
