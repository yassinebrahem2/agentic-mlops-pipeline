import os
from pathlib import Path

import pytest

from mlops_agents.integrations.github.auth import GhCli
from mlops_agents.integrations.github.git import GitRunner

pytestmark = pytest.mark.live


async def test_clone_private_repo_with_gh_token(tmp_path: Path) -> None:
    repo = os.environ.get("MLOPS_AGENTS_LIVE_GITHUB_REPO")
    if not repo:
        pytest.skip("set MLOPS_AGENTS_LIVE_GITHUB_REPO=owner/name (a private repo you can read)")
    gh = GhCli()
    runner = GitRunner(tmp_path / "bin", gh.token)
    await runner.run(
        ["clone", "--depth", "1", f"https://github.com/{repo}.git", str(tmp_path / "c")],
        authenticated=True,
    )
    assert (tmp_path / "c" / ".git").is_dir()
    assert "x-access-token" not in (tmp_path / "c" / ".git" / "config").read_text()
