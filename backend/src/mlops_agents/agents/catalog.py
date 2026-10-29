from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mlops_agents.agents.hooks.names import STATUS_BOARD_HOOK
from mlops_agents.errors import DomainError

ORCHESTRATOR = "orchestrator"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_steps: int | None = Field(default=25, ge=1)


class AgentDefinition(BaseModel):
    """One agent, as written in agents/definitions/<name>.md. Only what is special about it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    chat: bool = True
    schedules: bool = False
    can_message: tuple[str, ...] = ()
    context_hooks: tuple[str, ...] = ()
    status_board: bool = False
    limits: Limits = Limits()
    prompt: str = Field(min_length=1)

    @property
    def hook_names(self) -> tuple[str, ...]:
        """The context hooks this agent gets: `status_board: true` counts as that hook until
        the definitions list it in `context_hooks` and the field is removed."""
        if self.status_board and STATUS_BOARD_HOOK not in self.context_hooks:
            return (*self.context_hooks, STATUS_BOARD_HOOK)
        return self.context_hooks


class DefinitionError(Exception):
    pass


def parse_definition(text: str, source: str) -> AgentDefinition:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise DefinitionError(f"{source}: must start with a '---' frontmatter block")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise DefinitionError(f"{source}: the frontmatter is not closed with '---'")
    try:
        meta: object = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise DefinitionError(f"{source}: invalid YAML in the frontmatter: {exc}") from None
    if not isinstance(meta, dict):
        raise DefinitionError(f"{source}: the frontmatter must be a mapping")
    try:
        return AgentDefinition.model_validate({**meta, "prompt": text[end + 5 :].strip()})  # pyright: ignore[reportUnknownArgumentType] -- YAML is untyped until validated
    except ValidationError as exc:
        raise DefinitionError(f"{source}: {exc}") from None


class AgentCatalog:
    def __init__(
        self,
        definitions: Iterable[AgentDefinition],
        tool_names: frozenset[str],
        *,
        hook_names: frozenset[str] = frozenset(),
    ) -> None:
        self._by_name: dict[str, AgentDefinition] = {}
        for definition in definitions:
            if definition.name in self._by_name:
                raise DefinitionError(f"agent {definition.name} is defined twice")
            self._by_name[definition.name] = definition
        for d in self._by_name.values():
            if unknown := sorted(set(d.tools) - tool_names):
                raise DefinitionError(f"agent {d.name} uses unknown tools: {', '.join(unknown)}")
            if unknown := sorted(set(d.can_message) - set(self._by_name)):
                raise DefinitionError(
                    f"agent {d.name} may message unknown agents: {', '.join(unknown)}"
                )
            if unknown := sorted(set(d.context_hooks) - hook_names):
                raise DefinitionError(
                    f"agent {d.name} uses unknown context hooks: {', '.join(unknown)}"
                )
            # A hook listed twice would render twice, concurrently.
            if twice := sorted(n for n, c in Counter(d.context_hooks).items() if c > 1):
                raise DefinitionError(
                    f"agent {d.name} lists context hooks twice: {', '.join(twice)}"
                )

    @classmethod
    def load(
        cls,
        tool_names: frozenset[str],
        extra_dirs: Iterable[Path] = (),
        *,
        hook_names: frozenset[str] = frozenset(),
    ) -> AgentCatalog:
        folder = files("mlops_agents.agents").joinpath("definitions")
        definitions = [
            parse_definition(entry.read_text("utf-8"), entry.name)
            for entry in sorted(folder.iterdir(), key=lambda e: e.name)
            if entry.name.endswith(".md")
        ]
        for extra in extra_dirs:
            definitions.extend(
                parse_definition(p.read_text("utf-8"), str(p)) for p in sorted(extra.glob("*.md"))
            )
        return cls(definitions, tool_names, hook_names=hook_names)

    def get(self, name: str) -> AgentDefinition:
        definition = self._by_name.get(name)
        if definition is None:
            raise DomainError("agent_not_found", f"There is no agent named {name}.", status=404)
        return definition

    def all(self) -> list[AgentDefinition]:
        return sorted(self._by_name.values(), key=lambda d: (d.name != ORCHESTRATOR, d.name))
