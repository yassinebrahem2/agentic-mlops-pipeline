from __future__ import annotations

import asyncio
import os
import re
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
    try:
        return re.compile(_translate(pattern), re.IGNORECASE if os.name == "nt" else 0)
    except re.error as exc:
        raise ToolFailure(f"Invalid glob pattern {pattern!r}: {exc}") from None


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
