import os
import subprocess
from pathlib import Path

import pytest

from mlops_agents.integrations.github.git import ASKPASS_ENV_VAR, GitError, GitRunner
from tests.conftest import make_repo


def test_askpass_answers_username_and_token(tmp_path: Path) -> None:
    runner = GitRunner(tmp_path / "bin")
    script = str(runner.askpass_path())
    env = {**os.environ, ASKPASS_ENV_VAR: "tok_123"}
    user = subprocess.run(
        [script, "Username for 'https://github.com': "],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    password = subprocess.run(
        [script, "Password for 'https://x-access-token@github.com': "],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert user.stdout.strip() == "x-access-token"
    assert password.stdout.strip() == "tok_123"


async def test_run_in_a_repository(tmp_path: Path, git_env: dict[str, str]) -> None:
    repo = make_repo(tmp_path / "r", {"a.txt": "hi"}, git_env)
    result = await GitRunner(tmp_path / "bin").run(["ls-files"], cwd=repo)
    assert result.stdout.split() == ["a.txt"]


async def test_failure_raises_with_scrubbed_message(tmp_path: Path) -> None:
    async def token() -> str | None:
        return "tok_secret_99"

    runner = GitRunner(tmp_path / "bin", token)
    with pytest.raises(GitError) as err:
        await runner.run(
            ["clone", "http://127.0.0.1:9/tok_secret_99/repo.git", str(tmp_path / "c")],
            authenticated=True,
            timeout=60,
        )
    assert "tok_secret_99" not in str(err.value)
    assert str(err.value).startswith("git clone failed")


async def test_unchecked_failure_returns_the_code(tmp_path: Path) -> None:
    result = await GitRunner(tmp_path / "bin").run(
        ["rev-parse", "--show-toplevel"], cwd=tmp_path, check=False
    )
    assert result.code != 0
