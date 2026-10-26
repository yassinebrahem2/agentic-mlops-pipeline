from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values

ASKPASS_ENV_VAR = "MLOPS_AGENTS_GIT_TOKEN"
TokenProvider = Callable[[], Awaitable[str | None]]

# git asks GIT_ASKPASS for a username, then a password. GitHub accepts any username with a
# token as the password. The token travels only in the child's environment.
_ASKPASS_POSIX = (
    '#!/bin/sh\ncase "$1" in\n  Username*) echo x-access-token ;;\n'
    '  *) printf "%s\\n" "$MLOPS_AGENTS_GIT_TOKEN" ;;\nesac\n'
)
# Passing the token through cmd's `echo` is safe only because GitHub tokens are
# `[A-Za-z0-9_]`; other token types need a different script.
_ASKPASS_WINDOWS = (
    '@echo off\r\necho %~1| findstr /b /c:"Username" >nul\r\n'
    "if %errorlevel%==0 (echo x-access-token) else (echo %MLOPS_AGENTS_GIT_TOKEN%)\r\n"
)


async def no_token() -> str | None:
    return None


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str
    stderr: str


class GitError(Exception):
    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


class GitRunner:
    """Runs git without ever prompting: no credential helpers, no terminal prompts, no GCM
    windows. Tokens reach git only through GIT_ASKPASS; output is scrubbed of them."""

    def __init__(self, bin_dir: Path, token_provider: TokenProvider = no_token) -> None:
        self._bin_dir = bin_dir
        self._token_provider = token_provider

    def askpass_path(self) -> Path:
        windows = os.name == "nt"
        path = self._bin_dir / ("git-askpass.cmd" if windows else "git-askpass.sh")
        content = _ASKPASS_WINDOWS if windows else _ASKPASS_POSIX
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            self._bin_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
            if not windows:
                path.chmod(0o700)
        return path

    async def run(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        *,
        check: bool = True,
        timeout: float = 600,  # noqa: ASYNC109 -- passed to subprocess.run in asyncio.to_thread
        authenticated: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> GitResult:
        token = await self._token_provider() if authenticated else None
        env = dict(os.environ)

        # Clear inherited author/committer overrides
        for key in list(env.keys()):
            if key.startswith("GIT_AUTHOR_") or key.startswith("GIT_COMMITTER_"):
                env.pop(key, None)

        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
                "GIT_ASKPASS": str(self.askpass_path()),
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            }
        )
        env.pop(ASKPASS_ENV_VAR, None)
        if token:
            env[ASKPASS_ENV_VAR] = token
        if extra_env:
            env.update(extra_env)
        command = ["git", "-c", "credential.helper=", "-c", "core.quotepath=off", *args]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=cwd,
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
            raise GitError(
                f"git {args[0]} took longer than {int(timeout)} seconds and was stopped.", code=-1
            ) from None
        hidden = [token] if token else []
        stdout = scrub_values(completed.stdout.decode("utf-8", "replace"), hidden)
        stderr = scrub_values(
            scrub_url_secrets(completed.stderr.decode("utf-8", "replace")), hidden
        )
        if check and completed.returncode != 0:
            reason = stderr.strip() or stdout.strip() or "no output"
            raise GitError(f"git {args[0]} failed: {reason}", code=completed.returncode)
        return GitResult(completed.returncode, stdout, stderr)
