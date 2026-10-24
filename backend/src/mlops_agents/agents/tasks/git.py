from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.integrations.github.git import ASKPASS_ENV_VAR, GitError, GitResult, GitRunner


def git_dir_for_task(repo_root: Path, task_id: str) -> Path:
    """Finds the worktree git dir for a task in repo_root.

    Never trusts the worktree's own .git entry. Instead, resolves through the
    repository's own git directory.
    """
    git_entry = repo_root / ".git"
    if git_entry.is_file():
        line = git_entry.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            target = Path(line.removeprefix("gitdir:").strip())
            if not target.is_absolute():
                target = (repo_root / target).resolve()
            common_file = target / "commondir"
            if common_file.exists():
                common = common_file.read_text(encoding="utf-8").strip()
                common_dir = (target / common).resolve()
                return common_dir / "worktrees" / task_id
            return target / "worktrees" / task_id
    return git_entry / "worktrees" / task_id


async def task_git(
    runner: GitRunner,
    *,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    args: Sequence[str],
    check: bool = True,
    timeout: float = 600,  # noqa: ASYNC109 -- passed to subprocess.run in asyncio.to_thread
    authenticated: bool = False,
) -> GitResult:
    """The only code that runs git on a task worktree.

    Every call:
    - sets GIT_DIR and GIT_WORK_TREE from the task record, never trusting .git;
    - passes -c core.hooksPath=<empty_hooks>, -c core.fsmonitor=false, -c core.longpaths=true;
    - sets GIT_SSH_COMMAND="ssh -o BatchMode=yes";
    - routes token through askpass when authenticated is True;
    - scrubs output of secrets.
    """
    token = await runner._token_provider() if authenticated else None  # pyright: ignore[reportPrivateUsage]
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": str(runner.askpass_path()),
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(worktree),
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        }
    )
    env.pop(ASKPASS_ENV_VAR, None)
    if token:
        env[ASKPASS_ENV_VAR] = token

    command = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.quotepath=off",
        "-c",
        f"core.hooksPath={empty_hooks}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.longpaths=true",
        *args,
    ]

    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=worktree,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise GitError(
            "git is not installed or not on PATH. Install git and restart the app.", code=-1
        ) from None
    except subprocess.TimeoutExpired:
        cmd_name = args[0] if args else "command"
        raise GitError(
            f"git {cmd_name} took longer than {int(timeout)} seconds and was stopped.", code=-1
        ) from None

    hidden = [token] if token else []
    stdout = scrub_values(completed.stdout.decode("utf-8", "replace"), hidden)
    stderr = scrub_values(scrub_url_secrets(completed.stderr.decode("utf-8", "replace")), hidden)
    if check and completed.returncode != 0:
        cmd_name = args[0] if args else "command"
        reason = stderr.strip() or stdout.strip() or "no output"
        raise GitError(f"git {cmd_name} failed: {reason}", code=completed.returncode)
    return GitResult(completed.returncode, stdout, stderr)
