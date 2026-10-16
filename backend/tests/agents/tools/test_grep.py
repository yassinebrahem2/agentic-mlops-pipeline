import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mlops_agents.agents.tools import grep
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.agents.tools.grep import MAX_FILE_BYTES, GrepTool
from mlops_agents.integrations.github.git import GitRunner
from tests.conftest import make_repo

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


def _commit_all(root: Path, env: dict[str, str]) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "more"], cwd=root, env=env, check=True, capture_output=True
    )


@pytest.mark.parametrize("rg", ENGINES)
async def test_tracked_symlink_to_outside_is_never_searched(
    tmp_path: Path, git_env: dict[str, str], rg: str | None
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private-marker-7731\n")
    root = make_repo(tmp_path / "repo", {"a.txt": "hello\n"}, git_env)
    try:
        (root / "notes.txt").symlink_to(outside)
    except OSError:
        pytest.skip("this machine does not allow creating symlinks")
    _commit_all(root, git_env)
    ctx = ToolContext(RepoFiles(root, GitRunner(tmp_path / "bin")))
    assert await ctx.files.tracked() == ["a.txt"]
    out = await GrepTool(rg).invoke(ctx, {"pattern": "private-marker-7731"})
    assert out.content == "No matches found."


@pytest.mark.parametrize("rg", ENGINES)
async def test_files_over_the_size_limit_are_named_in_a_note(
    tmp_path: Path, git_env: dict[str, str], rg: str | None
) -> None:
    root = make_repo(tmp_path / "repo", {"small.csv": "needle\n"}, git_env)
    (root / "large.csv").write_bytes(b"needle\n" + b"x" * MAX_FILE_BYTES)
    _commit_all(root, git_env)
    ctx = ToolContext(RepoFiles(root, GitRunner(tmp_path / "bin")))
    out = await GrepTool(rg).invoke(ctx, {"pattern": "needle"})
    lines = out.content.splitlines()
    assert lines[0] == "small.csv"
    assert lines[-1].startswith("[Skipped 1 files larger than")
