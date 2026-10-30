"""Constants for monitoring action hooks."""

from __future__ import annotations

from mlops_agents.config import ENV_PREFIX
from mlops_agents.sandbox.contract import MAX_BASH_TIMEOUT_S

HOOK_DETAIL_MAX_CHARS: int = 2000
HOOK_CREDENTIAL_ENV: str = f"{ENV_PREFIX}HOOK_CREDENTIAL"
HOOK_COMMAND_TIMEOUT_S: int = MAX_BASH_TIMEOUT_S
