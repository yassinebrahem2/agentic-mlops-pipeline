from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
    ToolRegistry,
)
from tests.agents.ctx import tool_context


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

    def invalidate(self) -> None:
        return None


CTX = tool_context(NoFiles())


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


def test_parse_returns_the_arguments_model() -> None:
    parsed = EchoTool().parse({"text": "hi", "-l": True})
    assert isinstance(parsed, EchoArgs)
    assert (parsed.text, parsed.loud) == ("hi", True)


def test_parse_returns_the_stage_1_error_for_invalid_arguments() -> None:
    parsed = EchoTool().parse({"txt": "hi"})
    assert isinstance(parsed, ToolOutput)
    assert parsed.is_error
    assert parsed.content == (
        "Invalid arguments for Echo: text: Field required; txt: Extra inputs are not permitted"
    )


async def test_default_preview_is_text_with_indented_json_arguments() -> None:
    args = EchoArgs.model_validate({"text": "hi", "-l": True})
    preview = await EchoTool().preview(CTX, args)
    assert preview == Preview(
        kind="text",
        title="Echo",
        body='{\n  "text": "hi",\n  "-l": true\n}',
    )
