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
from mlops_agents.agents.tools.files import norm_path
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


def _too_large(path: Path) -> bool:
    try:
        return path.stat().st_size > MAX_FILE_BYTES
    except OSError:
        return False


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
    rg: str, root: Path, prefix: str, files: list[str], args: GrepArgs, before: int, after: int
) -> SearchResult:
    # rg searches the folder and its own ignore rules apply; results are then limited to the
    # files the agent may see. Tracked files that .gitignore also lists are not searched by rg.
    command = [rg, "--json", "--no-config", "--hidden", "--max-filesize", str(MAX_FILE_BYTES)]
    command += ["-e", args.pattern]
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
    if completed.returncode == 2 and not completed.stdout:
        message = completed.stderr.decode("utf-8", "replace").strip()
        if "regex" in message.lower():
            raise ToolFailure(f"Invalid regular expression: {message}")
        raise ToolFailure(f"The search failed: {message}")
    lookup = {norm_path(rel): rel for rel in files}
    result = SearchResult(skipped=sum(1 for rel in files if _too_large(root / rel)))
    for raw in completed.stdout.splitlines():
        event = _RgEvent.model_validate_json(raw)
        if event.type not in ("match", "context"):
            continue
        data = _RgLine.model_validate(event.data)
        rel = Path(data.path.decoded()).relative_to(root).as_posix()
        canonical = lookup.get(norm_path(rel))
        if canonical is None or data.line_number is None:
            continue
        for k, part in enumerate(data.lines.decoded().rstrip("\r\n").split("\n")):
            hit = Hit(data.line_number + k, part.rstrip("\r"), event.type == "match")
            result.hits.setdefault(canonical, []).append(hit)
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
        lines.append(
            f"[Skipped {result.skipped} files larger than {MAX_FILE_BYTES:,} bytes; "
            "Read them in parts instead.]"
        )
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
            single = await ctx.files.resolve_file(args.path)
            return single, [single]
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
        if not files:
            return ToolOutput(_format(SearchResult(), args))
        before = args.before if args.before is not None else (args.context or 0)
        after = args.after if args.after is not None else (args.context or 0)
        if self._rg is not None:
            result = await asyncio.to_thread(
                _search_rg, self._rg, ctx.files.root, prefix, files, args, before, after
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
