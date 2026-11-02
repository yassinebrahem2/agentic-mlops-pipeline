"""What the `connections` and `handoff` proposal kinds share: their key and their part runner."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Final

from mlops_agents.credentials.scrub import error_text, scrub_url_secrets, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.proposals.contract import ApplyResult

logger = logging.getLogger(__name__)

# `key` receives only the payload, which holds no project ID, and proposals are stored per
# project database: one constant keys one pending proposal per kind per project.
PROJECT_KEY: Final = "project"

UNEXPECTED_REASON: Final = "unexpected error, see the log"

SecretValues = Callable[[], Awaitable[list[str]]]


class Parts:
    """Runs each part of an apply on its own: a part that fails never stops the next."""

    def __init__(self, secret_values: SecretValues) -> None:
        self._secret_values = secret_values
        self.applied: list[str] = []
        self.failed: dict[str, str] = {}

    async def run(
        self, part: str, action: Callable[[], Awaitable[object]], *, listed: bool = True
    ) -> bool:
        """True when the part applied; otherwise its reason is recorded under `failed`. A part
        not `listed` (housekeeping) shows only when it fails."""
        try:
            await action()
        except DomainError as exc:
            await self.fail(part, exc.message)
            return False
        except Exception as exc:
            err = await self._scrub(part, error_text(exc))
            logger.error("The proposal part %s failed unexpectedly: %s", part, err)
            self.failed[part] = UNEXPECTED_REASON
            return False
        if listed:
            self.applied.append(part)
        return True

    async def _scrub(self, part: str, text: str) -> str:
        try:
            secrets = await self._secret_values()
        except Exception as exc:
            logger.error(
                "The secret values are unknown; the reason of part %s is kept with only its URL "
                "secrets masked: %s",
                part,
                error_text(exc),
            )
            return scrub_url_secrets(text)
        return scrub_values(text, secrets)

    async def fail(self, part: str, reason: str) -> None:
        """Records `reason` with every known secret masked. Without the secret values, the
        reason is still kept, with the secrets a URL can carry masked."""
        self.failed[part] = await self._scrub(part, reason)

    def result(self) -> ApplyResult:
        return ApplyResult(applied=list(self.applied), failed=dict(self.failed))
