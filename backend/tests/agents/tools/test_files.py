import asyncio
import os
import subprocess
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


def _commit_new_file(repo: Path, git_env: dict[str, str]) -> None:
    (repo / "added.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "added.py"], cwd=repo, env=git_env, check=True)
    subprocess.run(["git", "commit", "-m", "add"], cwd=repo, env=git_env, check=True)


async def test_invalidate_drops_the_cached_listing(
    ctx: ToolContext, repo: Path, git_env: dict[str, str]
) -> None:
    assert "added.py" not in await ctx.files.tracked()
    await asyncio.to_thread(_commit_new_file, repo, git_env)

    assert "added.py" not in await ctx.files.tracked()  # still the cached listing

    ctx.files.invalidate()

    assert "added.py" in await ctx.files.tracked()
    assert await ctx.files.resolve_file("added.py") == "added.py"
