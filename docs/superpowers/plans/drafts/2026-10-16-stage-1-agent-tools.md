# Stage 1 — Agent Tools Implementation Plan (DRAFT)

> **DRAFT: not ready to implement.** This plan sits in `drafts/` until both items below are done. Then it moves to `docs/superpowers/plans/` on its own `plan/stage-1-<topic>` branch and gets its issues.
> 1. **Advisor review.** Round 1 found blockers, and they are fixed. Rounds must continue until one finds no blocking issues.
> 2. **Check against the merged backend-core code.** backend-core changed while it was implemented (see PR #10, "done differently"). Every name in this plan's Consumes lines and test fixtures must match the code on `main`.


> **For agentic workers:** Implement this plan task by task, in order. Each task lists its exact files, the interfaces it consumes and produces, its tests (the task's stop condition) and the commands that prove it works. Claude Code users may use superpowers:executing-plans or superpowers:subagent-driven-development; nothing here requires them. Steps use checkbox (`- [ ]`) syntax. Team rules (claiming tasks, commits, PRs) are in `AGENTS.md`.

**Goal:** Agent definition files and the catalog, the tool registry with risk classes, the tracked-file filter, and the Read, Glob and Grep tools with Claude Code's argument shapes.

**Branch:** `plan/stage-1-agent-tools`. **Depends on:** backend-core (branch from main after it is merged).

**Read first:** `docs/superpowers/plans/2026-10-16-stage-1-overview.md` (global constraints, review focus, file structure), the spec `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md`, and `docs/roadmap.md`. The global constraints apply to every task below.

## Tasks

| Task | Title | Depends on |
|---|---|---|
| 1 | Agent catalog and tool registry | none |
| 2 | Repo file filter and Read/Glob/Grep | 1 |

---

### Task 1: Agent catalog and tool registry

**Files:**
- Create: `backend/src/mlops_agents/agents/__init__.py` (empty), `agents/catalog.py`, `agents/definitions/orchestrator.md`, `agents/tools/__init__.py` (empty for now; Task 2 fills it), `agents/tools/base.py`
- Test: `backend/tests/agents/__init__.py` (empty), `test_catalog.py`, `test_tool_base.py`

**Interfaces:**
- Consumes: `DomainError` (backend-core plan, Task 2).
- Produces:
  - `tools/base.py`: `class RiskClass(StrEnum)` (`READ`, `WRITE_CODE`, `RUN`, `MESSAGE`, `GITHUB`, `DEPLOY`, values as in `docs/roadmap.md`), `class ToolFailure(Exception)`, frozen dataclass `ToolOutput(content: str, is_error: bool = False)`, `class FileAccess(Protocol)` (`root: Path` property, `async tracked() -> list[str]`, `async under(prefix: str) -> list[str]`, `async resolve_file(user_path: str) -> str`, `async resolve_dir(user_path: str | None) -> str`), frozen dataclass `ToolContext(files: FileAccess)`, `class AgentTool[A: BaseModel](ABC)` with class vars `name`, `description`, `risk`, `args_model` and methods `async run(ctx, args: A) -> ToolOutput` (abstract), `schema() -> dict[str, Any]`, `async invoke(ctx, raw_args: Mapping[str, Any]) -> ToolOutput`, `class ToolRegistry(tools=())` with `register(tool)`, `get(name) -> AgentTool[Any] | None`, `names() -> frozenset[str]`, `schemas(names: Iterable[str]) -> list[dict[str, Any]]`.
  - `catalog.py`: `Limits(max_steps: int | None = 25)`, `AgentDefinition(name, display_name, description, tools, chat, schedules, can_message, context_hooks, limits, prompt)`, `class DefinitionError(Exception)`, `KNOWN_HOOKS: frozenset[str]` (empty in stage 1), `parse_definition(text: str, source: str) -> AgentDefinition`, `AgentCatalog(definitions, tool_names: frozenset[str])` with classmethod `load(tool_names, extra_dirs: Iterable[Path] = ()) -> AgentCatalog`, `get(name) -> AgentDefinition`, `all() -> list[AgentDefinition]` (Orchestrator first, then by name).

- [ ] **Step 1: Write the failing tests**

`backend/tests/agents/test_tool_base.py`:

```python
from pathlib import Path
from typing import ClassVar

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
```

`backend/tests/agents/test_catalog.py`:

```python
from pathlib import Path

import pytest

from mlops_agents.agents.catalog import AgentCatalog, DefinitionError, parse_definition
from mlops_agents.errors import DomainError

TOOLS = frozenset({"Read", "Glob", "Grep"})

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


def test_packaged_orchestrator() -> None:
    catalog = AgentCatalog.load(TOOLS)
    orchestrator = catalog.get("orchestrator")
    assert orchestrator.tools == ("Read", "Glob", "Grep")
    assert orchestrator.chat
    assert catalog.all()[0].name == "orchestrator"
    with pytest.raises(DomainError) as err:
        catalog.get("nobody")
    assert err.value.code == "agent_not_found"


def test_adding_an_agent_is_adding_a_file(tmp_path: Path) -> None:
    (tmp_path / "helper.md").write_text(VALID, encoding="utf-8")
    catalog = AgentCatalog.load(TOOLS, [tmp_path])
    assert [d.name for d in catalog.all()] == ["orchestrator", "helper"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/agents -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mlops_agents.agents'`.

- [ ] **Step 3: Implement**

`backend/src/mlops_agents/agents/tools/base.py`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

from pydantic import BaseModel, ValidationError


class RiskClass(StrEnum):
    """Tool risk classes from docs/roadmap.md. The permission policy (stage 2) keys on these."""

    READ = "read"
    WRITE_CODE = "write_code"
    RUN = "run"
    MESSAGE = "message"
    GITHUB = "github"
    DEPLOY = "deploy"


class ToolFailure(Exception):
    """An expected failure the agent should read and react to, such as a missing file."""


@dataclass(frozen=True)
class ToolOutput:
    content: str
    is_error: bool = False


class FileAccess(Protocol):
    @property
    def root(self) -> Path: ...

    async def tracked(self) -> list[str]: ...

    async def under(self, prefix: str) -> list[str]: ...

    async def resolve_file(self, user_path: str) -> str: ...

    async def resolve_dir(self, user_path: str | None) -> str: ...


@dataclass(frozen=True)
class ToolContext:
    files: FileAccess


def _summary(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    )


class AgentTool[A: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[RiskClass]
    args_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, ctx: ToolContext, args: A) -> ToolOutput: ...

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(by_alias=True),
            },
        }

    async def invoke(self, ctx: ToolContext, raw_args: Mapping[str, Any]) -> ToolOutput:
        try:
            args = cast(A, self.args_model.model_validate(dict(raw_args)))
        except ValidationError as exc:
            return ToolOutput(f"Invalid arguments for {self.name}: {_summary(exc)}", is_error=True)
        try:
            return await self.run(ctx, args)
        except ToolFailure as exc:
            return ToolOutput(str(exc), is_error=True)


class ToolRegistry:
    def __init__(self, tools: Iterable[AgentTool[Any]] = ()) -> None:
        self._tools: dict[str, AgentTool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} is registered twice")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool[Any] | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def schemas(self, names: Iterable[str]) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in names]
```

`backend/src/mlops_agents/agents/catalog.py`:

```python
from __future__ import annotations

from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mlops_agents.errors import DomainError

ORCHESTRATOR = "orchestrator"
# Context hooks (data commands) arrive in stage 3; until then no definition may use one.
KNOWN_HOOKS: frozenset[str] = frozenset()


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
    limits: Limits = Limits()
    prompt: str = Field(min_length=1)


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
    def __init__(self, definitions: Iterable[AgentDefinition], tool_names: frozenset[str]) -> None:
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
            if unknown := sorted(set(d.context_hooks) - KNOWN_HOOKS):
                raise DefinitionError(
                    f"agent {d.name} uses unknown context hooks: {', '.join(unknown)}"
                )

    @classmethod
    def load(cls, tool_names: frozenset[str], extra_dirs: Iterable[Path] = ()) -> AgentCatalog:
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
        return cls(definitions, tool_names)

    def get(self, name: str) -> AgentDefinition:
        definition = self._by_name.get(name)
        if definition is None:
            raise DomainError("agent_not_found", f"There is no agent named {name}.", status=404)
        return definition

    def all(self) -> list[AgentDefinition]:
        return sorted(self._by_name.values(), key=lambda d: (d.name != ORCHESTRATOR, d.name))
```

`backend/src/mlops_agents/agents/definitions/orchestrator.md`:

```markdown
---
name: orchestrator
display_name: Orchestrator
description: The project's assistant. Knows the whole project and answers questions about it.
tools: [Read, Glob, Grep]
chat: true
schedules: false
can_message: []
context_hooks: []
limits:
  max_steps: 25
---
You are the Orchestrator for the software project described below. You are the user's main
assistant for this project: you know its code and you help them understand it.

How to work:
- Answer from the repository, not from memory. Use Glob to find files, Grep to search their
  contents, and Read to open a file before you describe it.
- Name the files you rely on with their paths, and line numbers when they matter.
- If something is not in the repository, say so plainly instead of guessing.
- You can only read files committed to git. Untracked files and secret files such as .env are
  not visible to you; if the user asks about them, explain that.
- You cannot change files, run commands or talk to other agents in this version. If the user
  asks for that, say it is not available yet.
- Keep answers short and concrete. Use Markdown for lists and code.
```

- [ ] **Step 4: Run tests and checks**

Run: `cd backend && uv run pytest -W error && uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/mlops_agents/agents backend/tests/agents
git commit -m "feat(agents): add agent definition files, catalog and tool registry"
```

---

### Task 2: Repo file filter and Read/Glob/Grep

**Files:**
- Create: `backend/src/mlops_agents/agents/tools/files.py`, `read.py`, `glob.py`, `grep.py`
- Modify: `backend/src/mlops_agents/agents/tools/__init__.py`
- Test: `backend/tests/agents/tools/__init__.py` (empty), `conftest.py`, `test_files.py`, `test_read.py`, `test_glob.py`, `test_grep.py`, `test_registry.py`

**Interfaces:**
- Consumes: `AgentTool`, `ToolContext`, `ToolOutput`, `ToolFailure`, `RiskClass`, `ToolRegistry`, `FileAccess` (Task 1), `GitRunner` (backend-core plan, Task 5); test fixture `git_env` and `make_repo(path, files, env) -> Path` from `tests/conftest.py` (backend-core plan, Task 5). Nothing from the providers or github-workspace plans: callers pass the repository root.
- Produces:
  - `files.py`: `DENYLIST: tuple[str, ...]`, `is_secret(rel_posix: str) -> bool`, `RepoFiles(root: Path, git: GitRunner)` implementing `FileAccess`. One instance per turn: the tracked-file list is cached on the instance.
  - `read.py`: `ReadArgs`, `ReadTool` (name `Read`), constants `MAX_LINES = 2000`, `MAX_LINE_CHARS = 2000`, `MAX_CHARS = 100_000`.
  - `glob.py`: `glob_to_regex(pattern: str) -> re.Pattern[str]`, `GlobArgs`, `GlobTool` (name `Glob`), `MAX_RESULTS = 200`.
  - `grep.py`: `TYPE_EXTENSIONS`, `GrepArgs`, `GrepTool(rg_path: str | None)` (name `Grep`; `None` means the pure-Python engine).
  - `tools/__init__.py`: `default_registry() -> ToolRegistry` with Read, Glob, Grep.
  - Path resolution (`resolve`, `is_file`), file reads and `rg` all run in `asyncio.to_thread`.
  - Both Grep engines give the same results: lines that are not UTF-8 are shown with replacement characters (`rg --json` sends them as base64 `bytes`), and binary files are not searched.
  - A search that runs past `RG_TIMEOUT_SECONDS` (60) returns an error result that says how to narrow it, never an exception.

Tool argument names and shapes follow Claude Code's Read (`file_path`, `offset`, `limit`), Glob (`pattern`, `path`) and Grep (`pattern`, `path`, `glob`, `type`, `output_mode`, `-i`, `-n`, `-A`, `-B`, `-C`, `head_limit`, `multiline`).

- [ ] **Step 1: Write the test repository fixture** `backend/tests/agents/tools/conftest.py`:

```python
import subprocess
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from tests.conftest import make_repo

APP_PY = "\n".join(
    [
        "import os",
        "",
        "def train(model):",
        "    return model.fit()",
        "",
        "def evaluate(model):",
        "    return model.score()",
    ]
)


@pytest.fixture
def repo(tmp_path: Path, git_env: dict[str, str]) -> Path:
    root = make_repo(
        tmp_path / "repo",
        {
            "README.md": "# Demo\nA demo project.\n",
            "src/app.py": APP_PY,
            "src/nested/util.py": "def helper():\n    return 1\n",
            "docs/guide.md": "Train the model with train().\n",
            ".env": "API_KEY=supersecret\n",
            "keys/id_rsa": "PRIVATE KEY\n",
            "big.txt": "\n".join(f"line {i}" for i in range(1, 2501)),
            "latin1.txt": "placeholder",
        },
        git_env,
    )
    (root / "latin1.txt").write_bytes(b"caf\xe9 au lait\n")
    (root / "data.bin").write_bytes(b"\x00\x01binary")
    subprocess.run(["git", "add", "-A"], cwd=root, env=git_env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "more"], cwd=root, env=git_env, check=True, capture_output=True
    )
    (root / "untracked.txt").write_text("not committed\n")
    return root


@pytest.fixture
def ctx(repo: Path, tmp_path: Path) -> ToolContext:
    return ToolContext(RepoFiles(repo, GitRunner(tmp_path / "bin")))
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/agents/tools/test_files.py`:

```python
import os
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import ToolContext, ToolFailure
from mlops_agents.agents.tools.files import is_secret


@pytest.mark.parametrize(
    "name", [".env", ".env.local", "prod.env", "a/b/id_rsa", "cert.PEM", "credentials-prod.json"]
)
def test_secret_names(name: str) -> None:
    assert is_secret(name)


@pytest.mark.parametrize("name", ["environment.py", "README.md", "src/keys.py"])
def test_ordinary_names(name: str) -> None:
    assert not is_secret(name)


async def test_tracked_excludes_secrets_and_untracked(ctx: ToolContext) -> None:
    tracked = await ctx.files.tracked()
    assert "src/app.py" in tracked
    assert ".env" not in tracked
    assert "keys/id_rsa" not in tracked
    assert "untracked.txt" not in tracked


async def test_resolve_file_rules(ctx: ToolContext, repo: Path) -> None:
    assert await ctx.files.resolve_file("src/app.py") == "src/app.py"
    assert await ctx.files.resolve_file(str(repo / "src" / "app.py")) == "src/app.py"
    with pytest.raises(ToolFailure, match="not a file committed"):
        await ctx.files.resolve_file("untracked.txt")
    with pytest.raises(ToolFailure, match="secret"):
        await ctx.files.resolve_file(".env")
    with pytest.raises(ToolFailure, match="outside the repository"):
        await ctx.files.resolve_file("../outside.txt")


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows behavior")
async def test_resolve_file_ignores_case_on_windows(ctx: ToolContext) -> None:
    assert await ctx.files.resolve_file("SRC/APP.PY") == "src/app.py"


async def test_symlink_escape_is_refused(ctx: ToolContext, repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    link = repo / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this machine does not allow creating symlinks")
    with pytest.raises(ToolFailure, match="outside the repository"):
        await ctx.files.resolve_file("link.txt")


async def test_resolve_dir(ctx: ToolContext) -> None:
    assert await ctx.files.resolve_dir(None) == ""
    assert await ctx.files.resolve_dir(".") == ""
    assert await ctx.files.resolve_dir("src") == "src/"
    assert await ctx.files.under("src/") == ["src/app.py", "src/nested/util.py"]
```

`backend/tests/agents/tools/test_read.py`:

```python
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.read import ReadTool


async def test_reads_with_line_numbers(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "src/app.py"})
    assert not out.is_error
    assert out.content.splitlines()[2] == "     3\tdef train(model):"


async def test_offset_and_limit(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "src/app.py", "offset": 3, "limit": 2})
    assert out.content.splitlines()[:2] == [
        "     3\tdef train(model):",
        "     4\t    return model.fit()",
    ]
    assert "continues after line 4" in out.content


async def test_large_file_is_cut_with_a_note(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "big.txt"})
    lines = out.content.splitlines()
    assert lines[1999].endswith("line 2000")
    assert "Read again with offset=2001" in lines[-1]


async def test_binary_and_non_utf8(ctx: ToolContext) -> None:
    binary = await ReadTool().invoke(ctx, {"file_path": "data.bin"})
    assert "binary file" in binary.content
    latin = await ReadTool().invoke(ctx, {"file_path": "latin1.txt"})
    assert "caf� au lait" in latin.content


async def test_errors_are_results(ctx: ToolContext) -> None:
    assert (await ReadTool().invoke(ctx, {"file_path": "untracked.txt"})).is_error
    assert (await ReadTool().invoke(ctx, {"path": "README.md"})).is_error
```

`backend/tests/agents/tools/test_glob.py`:

```python
import pytest

from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.glob import GlobTool, glob_to_regex


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.py", "src/nested/util.py", True),
        ("**/*.py", "app.py", True),
        ("*.py", "src/app.py", False),
        ("src/*.py", "src/app.py", True),
        ("src/*.py", "src/nested/util.py", False),
        ("**/*.{md,py}", "docs/guide.md", True),
        ("?.txt", "a.txt", True),
        ("[!a]*.txt", "big.txt", True),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert (glob_to_regex(pattern).fullmatch(path) is not None) == expected


async def test_glob_lists_tracked_matches(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "**/*.py"})
    assert sorted(out.content.splitlines()) == ["src/app.py", "src/nested/util.py"]


async def test_glob_relative_to_path(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "*.py", "path": "src"})
    assert out.content.splitlines() == ["src/app.py"]


async def test_glob_never_lists_secrets_or_untracked(ctx: ToolContext) -> None:
    out = await GlobTool().invoke(ctx, {"pattern": "**"})
    assert ".env" not in out.content.splitlines()
    assert "untracked.txt" not in out.content


async def test_no_match(ctx: ToolContext) -> None:
    assert (await GlobTool().invoke(ctx, {"pattern": "**/*.rs"})).content == "No files matched."
```

`backend/tests/agents/tools/test_grep.py`:

```python
import os
import shutil
from pathlib import Path

import pytest

from mlops_agents.agents.tools import grep
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.grep import GrepTool

ENGINES = [pytest.param(None, id="python")]
RG = shutil.which("rg")
ENGINES.append(
    pytest.param(
        RG, id="ripgrep", marks=pytest.mark.skipif(RG is None, reason="rg is not installed")
    )
)


@pytest.mark.parametrize("rg", ENGINES)
async def test_files_with_matches_by_default(ctx: ToolContext, rg: str | None) -> None:
    out = await GrepTool(rg).invoke(ctx, {"pattern": "def train"})
    assert out.content.splitlines() == ["src/app.py"]


@pytest.mark.parametrize("rg", ENGINES)
async def test_content_with_context(ctx: ToolContext, rg: str | None) -> None:
    out = await GrepTool(rg).invoke(
        ctx, {"pattern": "def train", "output_mode": "content", "-A": 1}
    )
    assert out.content.splitlines() == [
        "src/app.py:3:def train(model):",
        "src/app.py-4-    return model.fit()",
    ]


@pytest.mark.parametrize("rg", ENGINES)
async def test_count_case_and_type(ctx: ToolContext, rg: str | None) -> None:
    out = await GrepTool(rg).invoke(
        ctx, {"pattern": "TRAIN", "-i": True, "output_mode": "count", "type": "py"}
    )
    assert out.content.splitlines() == ["src/app.py:1"]


@pytest.mark.parametrize("rg", ENGINES)
async def test_glob_filter_and_path(ctx: ToolContext, rg: str | None) -> None:
    out = await GrepTool(rg).invoke(ctx, {"pattern": "train", "glob": "*.md"})
    assert out.content.splitlines() == ["docs/guide.md"]
    out = await GrepTool(rg).invoke(ctx, {"pattern": "return", "path": "src/nested"})
    assert out.content.splitlines() == ["src/nested/util.py"]


@pytest.mark.parametrize("rg", ENGINES)
async def test_secrets_and_untracked_are_never_searched(ctx: ToolContext, rg: str | None) -> None:
    assert (
        await GrepTool(rg).invoke(ctx, {"pattern": "supersecret"})
    ).content == "No matches found."
    assert (
        await GrepTool(rg).invoke(ctx, {"pattern": "not committed"})
    ).content == "No matches found."


@pytest.mark.parametrize("rg", ENGINES)
async def test_invalid_regex_is_an_error_result(ctx: ToolContext, rg: str | None) -> None:
    out = await GrepTool(rg).invoke(ctx, {"pattern": "(unclosed"})
    assert out.is_error
    assert "Invalid regular expression" in out.content


async def test_head_limit(ctx: ToolContext) -> None:
    out = await GrepTool(None).invoke(
        ctx, {"pattern": "line", "output_mode": "content", "head_limit": 5, "path": "big.txt"}
    )
    lines = out.content.splitlines()
    assert len(lines) == 6
    assert lines[-1].startswith("[Showing the first 5")


@pytest.mark.parametrize("rg", ENGINES)
async def test_non_utf8_lines_are_shown_with_replacement(ctx: ToolContext, rg: str | None) -> None:
    out = await GrepTool(rg).invoke(ctx, {"pattern": "au lait", "output_mode": "content"})
    assert out.content == "latin1.txt:1:caf\ufffd au lait"


@pytest.mark.parametrize("rg", ENGINES)
async def test_binary_files_are_not_searched(ctx: ToolContext, rg: str | None) -> None:
    assert (await GrepTool(rg).invoke(ctx, {"pattern": "binary"})).content == "No matches found."


@pytest.mark.skipif(os.name == "nt", reason="the stand-in rg is a POSIX shell script")
async def test_slow_search_is_stopped_with_a_note(
    ctx: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow_rg = tmp_path / "slow-rg"
    slow_rg.write_text("#!/bin/sh\nsleep 5\n")
    slow_rg.chmod(0o700)
    monkeypatch.setattr(grep, "RG_TIMEOUT_SECONDS", 0.2)
    out = await GrepTool(str(slow_rg)).invoke(ctx, {"pattern": "train"})
    assert out.is_error
    assert "took longer than 0.2 seconds" in out.content
```

`backend/tests/agents/tools/test_registry.py`:

```python
from mlops_agents.agents.tools import default_registry


def test_default_registry_has_the_stage_1_tools() -> None:
    assert default_registry().names() == frozenset({"Read", "Glob", "Grep"})
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/agents/tools -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mlops_agents.agents.tools.files'`.

- [ ] **Step 4: Implement**

`backend/src/mlops_agents/agents/tools/files.py`:

```python
from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path

from mlops_agents.agents.tools.base import ToolFailure
from mlops_agents.integrations.github.git import GitRunner

# File names that usually hold secrets. Matched case-insensitively against the base name.
# Known limitation (spec): a secret inside a tracked file with another name can still be read.
DENYLIST: tuple[str, ...] = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", "credentials*.json", "*secret*.json",
)  # fmt: skip


def is_secret(rel_posix: str) -> bool:
    name = rel_posix.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in DENYLIST)


def _norm(rel: str) -> str:
    return rel.casefold() if os.name == "nt" else rel


class RepoFiles:
    """What an agent may see in a repository: files tracked by git, minus secrets, never
    outside the repository root (no `..`, no symlink escapes). One instance per turn."""

    def __init__(self, root: Path, git: GitRunner) -> None:
        self._root = root
        self._resolved_root: Path | None = None
        self._git = git
        self._tracked: list[str] | None = None
        self._lookup: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._root

    async def tracked(self) -> list[str]:
        if self._tracked is None:
            result = await self._git.run(["ls-files", "-z"], cwd=self._root)
            self._tracked = sorted(p for p in result.stdout.split("\0") if p and not is_secret(p))
            self._lookup = {_norm(p): p for p in self._tracked}
        return self._tracked

    async def under(self, prefix: str) -> list[str]:
        files = await self.tracked()
        if not prefix:
            return files
        wanted = _norm(prefix)
        return [f for f in files if _norm(f).startswith(wanted)]

    def _relative(self, user_path: str) -> str:
        """Blocking (resolves symlinks): call it through asyncio.to_thread."""
        if self._resolved_root is None:
            self._resolved_root = self._root.resolve()
        candidate = Path(user_path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._resolved_root):
            raise ToolFailure(
                "That path is outside the repository. Only files inside the project can be read."
            )
        return resolved.relative_to(self._resolved_root).as_posix()

    async def resolve_file(self, user_path: str) -> str:
        rel = await asyncio.to_thread(self._relative, user_path)
        if is_secret(rel):
            raise ToolFailure(f"{rel} looks like a secret file, so it cannot be read.")
        await self.tracked()
        actual = self._lookup.get(_norm(rel))
        if actual is None:
            raise ToolFailure(
                f"{rel} is not a file committed to the repository. Only committed files can be "
                "read; use Glob to find files."
            )
        return actual

    async def resolve_dir(self, user_path: str | None) -> str:
        if not user_path or user_path.strip() in (".", "./"):
            return ""
        rel = await asyncio.to_thread(self._relative, user_path)
        return "" if rel == "." else rel.rstrip("/") + "/"
```

`backend/src/mlops_agents/agents/tools/read.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)

MAX_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_CHARS = 100_000
BINARY_SNIFF = 8192


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(
        description="Path of the file, relative to the repository root. Absolute paths inside the repository also work."
    )
    offset: int | None = Field(
        default=None,
        ge=1,
        description="Line number to start from (1-based). Use it for large files.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LINES,
        description=f"Number of lines to read. Defaults to {MAX_LINES}.",
    )


def _read_lines(path: Path, offset: int, limit: int) -> tuple[list[str], bool, bool]:
    """(numbered lines, is_binary, more_lines_follow). Stops early; never loads the whole file."""
    with path.open("rb") as handle:
        if b"\0" in handle.read(BINARY_SNIFF):
            return [], True, False
        handle.seek(0)
        out: list[str] = []
        chars = 0
        for number, raw in enumerate(handle, start=1):
            if number < offset:
                continue
            if len(out) >= limit or chars >= MAX_CHARS:
                return out, False, True
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " [line cut]"
            out.append(f"{number:>6}\t{line}")
            chars += len(line)
        return out, False, False


class ReadTool(AgentTool[ReadArgs]):
    name: ClassVar[str] = "Read"
    description: ClassVar[str] = (
        "Read a text file committed to the repository. Returns lines prefixed with their line "
        "numbers. Use offset and limit to read large files in parts."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = ReadArgs

    async def run(self, ctx: ToolContext, args: ReadArgs) -> ToolOutput:
        rel = await ctx.files.resolve_file(args.file_path)
        offset = args.offset or 1
        try:
            lines, binary, more = await asyncio.to_thread(
                _read_lines, ctx.files.root / rel, offset, args.limit or MAX_LINES
            )
        except OSError as exc:
            raise ToolFailure(f"{rel} could not be read: {exc.strerror or exc}") from None
        if binary:
            return ToolOutput(f"{rel} is a binary file; its contents are not shown.")
        if not lines:
            return ToolOutput(
                f"{rel} has no lines from line {offset}." if offset > 1 else f"{rel} is empty."
            )
        text = "\n".join(lines)
        if more:
            last = offset + len(lines) - 1
            text += f"\n[{rel} continues after line {last}. Read again with offset={last + 1} to see more.]"
        return ToolOutput(text)
```

`backend/src/mlops_agents/agents/tools/glob.py`:

```python
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput

MAX_RESULTS = 200


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
                out.append("(?:.*/)?")
            else:
                out.append(".*")
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (end := pattern.find("]", i + 1)) != -1:
            body = pattern[i + 1 : end]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = end
        elif c == "{" and (end := pattern.find("}", i + 1)) != -1:
            out.append(
                "(?:" + "|".join(_translate(alt) for alt in pattern[i + 1 : end].split(",")) + ")"
            )
            i = end
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` crosses folders, `*` and `?` do not, `[...]`, `[!...]` and `{a,b}` work as in shells."""
    return re.compile(_translate(pattern), re.IGNORECASE if os.name == "nt" else 0)


class GlobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(
        description="Glob matched against paths relative to `path`, e.g. **/*.py, src/**/*.ts, or *.md (top level only)."
    )
    path: str | None = Field(
        default=None, description="Folder to search in. Defaults to the repository root."
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class GlobTool(AgentTool[GlobArgs]):
    name: ClassVar[str] = "Glob"
    description: ClassVar[str] = (
        "Find files committed to the repository by path pattern. Returns matching paths, most "
        "recently modified first."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = GlobArgs

    async def run(self, ctx: ToolContext, args: GlobArgs) -> ToolOutput:
        prefix = await ctx.files.resolve_dir(args.path)
        regex = glob_to_regex(args.pattern)
        matches = [f for f in await ctx.files.under(prefix) if regex.fullmatch(f[len(prefix) :])]
        if not matches:
            return ToolOutput("No files matched.")
        root = ctx.files.root
        times = await asyncio.to_thread(lambda: {f: _mtime(root / f) for f in matches})
        ordered = sorted(matches, key=lambda f: (-times[f], f))
        text = "\n".join(ordered[:MAX_RESULTS])
        if len(ordered) > MAX_RESULTS:
            text += (
                f"\n[{len(ordered)} files matched; showing the {MAX_RESULTS} most recently "
                "modified. Narrow the pattern to see the others.]"
            )
        return ToolOutput(text)
```

`backend/src/mlops_agents/agents/tools/grep.py`:

```python
from __future__ import annotations

import asyncio
import base64
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.agents.tools.glob import glob_to_regex

MAX_FILE_BYTES = 5_000_000
RG_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_LINE = 500
TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "py": (".py", ".pyi"),
    "ipynb": (".ipynb",),
    "js": (".js", ".mjs", ".cjs", ".jsx"),
    "ts": (".ts", ".tsx", ".mts", ".cts"),
    "md": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "sql": (".sql",),
    "sh": (".sh", ".bash"),
    "txt": (".txt",),
    "csv": (".csv",),
    "html": (".html", ".htm"),
    "css": (".css",),
}


class GrepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    pattern: str = Field(description="Regular expression to search for.")
    path: str | None = Field(
        default=None, description="File or folder to search in. Defaults to the whole repository."
    )
    glob: str | None = Field(
        default=None, description="Only search files matching this glob, e.g. *.py or src/**/*.ts."
    )
    type: str | None = Field(
        default=None,
        description=f"Only search one file type: {', '.join(sorted(TYPE_EXTENSIONS))}.",
    )
    output_mode: Literal["content", "files_with_matches", "count"] = Field(
        default="files_with_matches",
        description="content: matching lines; files_with_matches: file paths; count: matches per file.",
    )
    case_insensitive: bool = Field(default=False, alias="-i", description="Ignore case.")
    line_numbers: bool = Field(
        default=True, alias="-n", description="Show line numbers (content mode)."
    )
    after: int | None = Field(
        default=None, alias="-A", ge=0, le=20, description="Lines to show after each match."
    )
    before: int | None = Field(
        default=None, alias="-B", ge=0, le=20, description="Lines to show before each match."
    )
    context: int | None = Field(
        default=None, alias="-C", ge=0, le=20, description="Lines to show around each match."
    )
    head_limit: int = Field(
        default=250, ge=1, le=1000, description="Maximum number of output lines."
    )
    multiline: bool = Field(default=False, description="Let the pattern match across lines.")


@dataclass(frozen=True)
class Hit:
    line_no: int
    text: str
    is_match: bool


@dataclass
class SearchResult:
    hits: dict[str, list[Hit]] = field(default_factory=dict[str, list[Hit]])
    skipped: int = 0


def _search_python(
    root: Path, files: list[str], regex: re.Pattern[str], before: int, after: int, multiline: bool
) -> SearchResult:
    result = SearchResult()
    for rel in files:
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                result.skipped += 1
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        lines = text.splitlines()
        if multiline:
            matched: set[int] = set()
            for m in regex.finditer(text):
                first = text.count("\n", 0, m.start())
                matched.update(range(first, text.count("\n", 0, m.end()) + 1))
        else:
            matched = {i for i, line in enumerate(lines) if regex.search(line)}
        if not matched:
            continue
        shown: set[int] = set()
        for i in matched:
            shown.update(range(max(0, i - before), min(len(lines), i + after + 1)))
        result.hits[rel] = [Hit(i + 1, lines[i], i in matched) for i in sorted(shown)]
    return result


class _RgText(BaseModel):
    """rg --json gives `text` for UTF-8 and base64 `bytes` for anything else."""

    text: str | None = None
    bytes: str | None = None

    def decoded(self) -> str:
        if self.text is not None:
            return self.text
        if self.bytes is not None:
            return base64.b64decode(self.bytes).decode("utf-8", "replace")
        return ""


class _RgLine(BaseModel):
    path: _RgText
    lines: _RgText
    line_number: int | None = None


class _RgEvent(BaseModel):
    type: str
    data: object = None


def _search_rg(
    rg: str, root: Path, prefix: str, allowed: set[str], args: GrepArgs, before: int, after: int
) -> SearchResult:
    # rg searches the folder and its own ignore rules apply; results are then limited to the
    # files the agent may see. Tracked files that .gitignore also lists are not searched by rg.
    command = [rg, "--json", "--no-config", "--hidden", "--max-filesize", "5M", "-e", args.pattern]
    if args.case_insensitive:
        command.append("-i")
    if args.multiline:
        command += ["-U", "--multiline-dotall"]
    if before:
        command += ["-B", str(before)]
    if after:
        command += ["-A", str(after)]
    command += ["--", str(root / prefix) if prefix else str(root)]
    try:
        completed = subprocess.run(
            command, cwd=root, capture_output=True, timeout=RG_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        raise ToolFailure(
            f"The search took longer than {RG_TIMEOUT_SECONDS:g} seconds and was stopped. "
            "Narrow it with path, glob or type."
        ) from None
    if completed.returncode == 2:
        message = completed.stderr.decode("utf-8", "replace").strip()
        if "regex" in message.lower():
            raise ToolFailure(f"Invalid regular expression: {message}")
        raise ToolFailure(f"The search failed: {message}")
    result = SearchResult()
    for raw in completed.stdout.splitlines():
        event = _RgEvent.model_validate_json(raw)
        if event.type not in ("match", "context"):
            continue
        data = _RgLine.model_validate(event.data)
        rel = Path(data.path.decoded()).relative_to(root).as_posix()
        if rel not in allowed or data.line_number is None:
            continue
        for k, part in enumerate(data.lines.decoded().rstrip("\r\n").split("\n")):
            hit = Hit(data.line_number + k, part.rstrip("\r"), event.type == "match")
            result.hits.setdefault(rel, []).append(hit)
    return result


def _format(result: SearchResult, args: GrepArgs) -> str:
    files = sorted(result.hits)
    lines: list[str]
    if not files:
        lines = ["No matches found."]
    elif args.output_mode == "files_with_matches":
        lines = files
    elif args.output_mode == "count":
        lines = [f"{f}:{sum(h.is_match for h in result.hits[f])}" for f in files]
    else:
        lines = []
        for f in files:
            previous: int | None = None
            for hit in result.hits[f]:
                if previous is not None and hit.line_no > previous + 1:
                    lines.append("--")
                sep = ":" if hit.is_match else "-"
                text = hit.text[:MAX_OUTPUT_LINE]
                lines.append(
                    f"{f}{sep}{hit.line_no}{sep}{text}" if args.line_numbers else f"{f}{sep}{text}"
                )
                previous = hit.line_no
    total = len(lines)
    if total > args.head_limit:
        lines = lines[: args.head_limit]
        lines.append(
            f"[Showing the first {args.head_limit} of {total} lines. Narrow the search or raise head_limit.]"
        )
    if result.skipped:
        lines.append(f"[Skipped {result.skipped} files larger than 5 MB.]")
    return "\n".join(lines)


class GrepTool(AgentTool[GrepArgs]):
    name: ClassVar[str] = "Grep"
    description: ClassVar[str] = (
        "Search the contents of files committed to the repository with a regular expression. "
        "Returns matching files by default; use output_mode=content to see the lines."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = GrepArgs

    def __init__(self, rg_path: str | None) -> None:
        self._rg = rg_path

    async def _candidates(self, ctx: ToolContext, args: GrepArgs) -> tuple[str, list[str]]:
        if args.path and await asyncio.to_thread((ctx.files.root / args.path).is_file):
            return "", [await ctx.files.resolve_file(args.path)]
        prefix = await ctx.files.resolve_dir(args.path)
        files = await ctx.files.under(prefix)
        if args.glob:
            regex = glob_to_regex(args.glob)
            basename_only = "/" not in args.glob
            files = [
                f for f in files
                if regex.fullmatch(f[len(prefix) :]) or (basename_only and regex.fullmatch(f.rsplit("/", 1)[-1]))
            ]  # fmt: skip
        if args.type:
            extensions = TYPE_EXTENSIONS.get(args.type)
            if extensions is None:
                raise ToolFailure(
                    f"Unknown type {args.type}. Known types: {', '.join(sorted(TYPE_EXTENSIONS))}."
                )
            files = [f for f in files if f.lower().endswith(extensions)]
        return prefix, files

    async def run(self, ctx: ToolContext, args: GrepArgs) -> ToolOutput:
        prefix, files = await self._candidates(ctx, args)
        before = args.before if args.before is not None else (args.context or 0)
        after = args.after if args.after is not None else (args.context or 0)
        if self._rg is not None:
            search_prefix = prefix if not args.path or prefix else ""
            result = await asyncio.to_thread(
                _search_rg, self._rg, ctx.files.root, search_prefix, set(files), args, before, after
            )
        else:
            flags = re.IGNORECASE if args.case_insensitive else 0
            if args.multiline:
                flags |= re.DOTALL | re.MULTILINE
            try:
                regex = re.compile(args.pattern, flags)
            except re.error as exc:
                raise ToolFailure(f"Invalid regular expression: {exc}") from None
            result = await asyncio.to_thread(
                _search_python, ctx.files.root, files, regex, before, after, args.multiline
            )
        return ToolOutput(_format(result, args))
```

`backend/src/mlops_agents/agents/tools/__init__.py`:

```python
import shutil

from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool


def default_registry() -> ToolRegistry:
    return ToolRegistry([ReadTool(), GlobTool(), GrepTool(shutil.which("rg"))])
```

- [ ] **Step 5: Run tests and checks**

Run: `cd backend && uv run pytest -W error && uv run ruff check && uv run ruff format --check && uv run pyright && uv build --wheel && unzip -l dist/*.whl | grep definitions/orchestrator.md`
Expected: all pass, and the wheel lists `mlops_agents/agents/definitions/orchestrator.md`. The `ripgrep` cases run where `rg` is installed (CI installs it) and are skipped with a stated reason elsewhere; both engines must give identical results on this fixture.

- [ ] **Step 6: Commit**

```bash
git add backend/src/mlops_agents/agents/tools backend/tests/agents/tools
git commit -m "feat(agents): add tracked-file filter and Read, Glob, Grep tools"
```
