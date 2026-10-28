from __future__ import annotations

import asyncio
import logging

import pytest

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.registry import HookRegistry

CTX = HookContext(project_id="prj_1", agent="orchestrator", trigger_text="hi")


class TextHook:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.text = text
        self.seen: list[HookContext] = []

    async def render(self, ctx: HookContext) -> str:
        self.seen.append(ctx)
        return self.text


class RaisingHook:
    def __init__(self, name: str, error: BaseException) -> None:
        self.name = name
        self.error = error

    async def render(self, ctx: HookContext) -> str:
        raise self.error


class WaitingHook:
    """Sets its own event, then waits for its partner's: two of them only finish together."""

    def __init__(self, name: str, own: asyncio.Event, other: asyncio.Event) -> None:
        self.name = name
        self.own = own
        self.other = other

    async def render(self, ctx: HookContext) -> str:
        self.own.set()
        await self.other.wait()
        return f"{self.name} done"


def _registry(*hooks: TextHook | RaisingHook | WaitingHook) -> HookRegistry:
    registry = HookRegistry()
    for hook in hooks:
        registry.register(hook)
    return registry


async def test_each_hook_text_is_wrapped_in_its_data_block() -> None:
    hook = TextHook("alpha", "line 1\nline 2")
    text = await _registry(hook).run(["alpha"], CTX)
    assert text == '<data source="hook:alpha">\nline 1\nline 2\n</data>'
    assert hook.seen == [CTX]


@pytest.mark.parametrize("closing", ["</data>", "</DATA>", "</Data >", "</data"])
async def test_a_hook_text_cannot_close_its_block_early(closing: str) -> None:
    hook = TextHook("alpha", f"before {closing} after")
    text = await _registry(hook).run(["alpha"], CTX)
    body = text.removeprefix('<data source="hook:alpha">\n').removesuffix("\n</data>")
    assert "</data" not in body.lower()
    assert body.startswith("before <\\/")
    assert body.endswith(" after")


async def test_output_follows_the_order_of_names() -> None:
    registry = _registry(TextHook("alpha", "A"), TextHook("beta", "B"))
    text = await registry.run(["beta", "alpha"], CTX)
    assert text == (
        '<data source="hook:beta">\nB\n</data>\n\n<data source="hook:alpha">\nA\n</data>'
    )


async def test_hooks_render_concurrently() -> None:
    first, second = asyncio.Event(), asyncio.Event()
    registry = _registry(WaitingHook("alpha", first, second), WaitingHook("beta", second, first))
    text = await asyncio.wait_for(registry.run(["alpha", "beta"], CTX), timeout=5)
    assert "alpha done" in text
    assert "beta done" in text


async def test_a_raising_hook_gives_one_line_and_the_others_still_render(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry(
        TextHook("alpha", "A"),
        RaisingHook("broken", RuntimeError("disk gone")),
        TextHook("beta", "B"),
    )
    with caplog.at_level(logging.ERROR):
        text = await registry.run(["alpha", "broken", "beta"], CTX)
    assert text == (
        '<data source="hook:alpha">\nA\n</data>\n\n'
        '<data source="hook:broken">\n[broken: unavailable (disk gone)]\n</data>\n\n'
        '<data source="hook:beta">\nB\n</data>'
    )
    failure = next(r for r in caplog.records if "broken" in r.getMessage())
    assert failure.exc_info is not None


async def test_a_raising_hook_error_text_is_scrubbed() -> None:
    error = RuntimeError("could not reach https://user:s3cret@example.com/x")
    text = await _registry(RaisingHook("broken", error)).run(["broken"], CTX)
    assert "s3cret" not in text
    assert text.startswith('<data source="hook:broken">\n[broken: unavailable (')


async def test_an_unknown_name_gives_its_line() -> None:
    text = await _registry(TextHook("alpha", "A")).run(["ghost"], CTX)
    assert text == '<data source="hook:ghost">\n[ghost: unknown context hook]\n</data>'


async def test_an_empty_rendering_hook_adds_nothing() -> None:
    registry = _registry(TextHook("alpha", "A"), TextHook("quiet", ""))
    text = await registry.run(["quiet", "alpha"], CTX)
    assert text == '<data source="hook:alpha">\nA\n</data>'


async def test_running_no_hooks_is_empty() -> None:
    assert await _registry(TextHook("alpha", "A")).run([], CTX) == ""


def test_names_lists_the_registered_hooks() -> None:
    registry = _registry(TextHook("alpha", "A"), TextHook("beta", "B"))
    assert registry.names() == frozenset({"alpha", "beta"})


def test_a_duplicate_register_raises() -> None:
    registry = _registry(TextHook("alpha", "A"))
    with pytest.raises(ValueError, match="alpha"):
        registry.register(TextHook("alpha", "again"))


async def test_a_cancel_from_a_hook_propagates() -> None:
    registry = _registry(TextHook("alpha", "A"), RaisingHook("stop", asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await registry.run(["alpha", "stop"], CTX)
