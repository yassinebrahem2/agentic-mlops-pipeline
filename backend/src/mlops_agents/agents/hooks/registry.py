"""The hook registry (I18): the one place agents' context hooks are registered and rendered."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence

from mlops_agents.agents.hooks.contract import ContextHook, HookContext
from mlops_agents.credentials.scrub import error_text

logger = logging.getLogger(__name__)

# Hook output is untrusted data: a data tag inside it, opening or closing, in any letter case or
# spacing around its "/", must not end its block early or forge another, so its "<" becomes "<\".
# Only the tag name "data" counts: "<database>" or "<dataset>" are ordinary text.
_DATA_TAG = re.compile(r"<(?=\s*/?\s*data(?![\w-]))", re.IGNORECASE)


def _block(name: str, text: str) -> str:
    return f'<data source="hook:{name}">\n{_DATA_TAG.sub(r"<\\", text)}\n</data>'


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, ContextHook] = {}

    def register(self, hook: ContextHook) -> None:
        if hook.name in self._hooks:
            raise ValueError(f"A context hook named {hook.name} is already registered.")
        self._hooks[hook.name] = hook

    def names(self) -> frozenset[str]:
        return frozenset(self._hooks)

    async def run(self, names: Sequence[str], ctx: HookContext) -> str:
        """The named hooks' texts, each in its marked block, in the order of `names`.

        Raises nothing a hook raises; a cancel of the caller (Stop) propagates, after every
        hook task has ended.
        """
        # A task group cancels and awaits every hook before it raises: none outlives the run.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._render(name, ctx)) for name in names]
        texts = [task.result() for task in tasks]
        return "\n\n".join(
            _block(name, text) for name, text in zip(names, texts, strict=True) if text
        )

    async def _render(self, name: str, ctx: HookContext) -> str:
        hook = self._hooks.get(name)
        if hook is None:
            return f"[{name}: unknown context hook]"
        try:
            return await hook.render(ctx)
        except asyncio.CancelledError as exc:
            # Only a cancel of this hook's task (the run's) goes on; a hook's own is its failure.
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
            return _unavailable(name, exc, error_text(exc) or "cancelled")
        except Exception as exc:
            return _unavailable(name, exc, error_text(exc))


def _unavailable(name: str, error: BaseException, reason: str) -> str:
    """A failed hook's one line, logged. A hook never raises (I18); this is the backstop, so
    one broken hook costs one line."""
    logger.error("Context hook %s failed", name, exc_info=error)
    return f"[{name}: unavailable ({reason})]"
