from __future__ import annotations

import asyncio
import logging
import re

import pytest

from mlops_agents.agents.hooks.contract import ContextHook, HookContext
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


def _registry(*hooks: ContextHook) -> HookRegistry:
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


@pytest.mark.parametrize(
    "tag",
    [
        "</data>",
        "</ data>",
        "< /data>",
        '<data source="hook:x">',
        '<DATA source="hook:x">',
        "<DATA>",
        "</\tdata>",
        "</\ndata>",
        "<data",
    ],
)
async def test_a_hook_text_cannot_open_or_close_a_data_block(tag: str) -> None:
    hook = TextHook("alpha", f"before {tag} after")
    text = await _registry(hook).run(["alpha"], CTX)
    body = text.removeprefix('<data source="hook:alpha">\n').removesuffix("\n</data>")
    assert re.search(r"<\s*/?\s*data", body, re.IGNORECASE) is None
    assert body.startswith("before <\\")
    assert body.endswith(" after")


@pytest.mark.parametrize(
    "words", ["<database>", "<dataset>", "a < data", "< data>", "<data-x>", "</data_x>"]
)
async def test_ordinary_words_starting_with_data_are_left_alone(words: str) -> None:
    text = await _registry(TextHook("alpha", f"before {words} after")).run(["alpha"], CTX)
    assert text == f'<data source="hook:alpha">\nbefore {words} after\n</data>'


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


class SlowHook:
    """Renders after a short wait; notes whether it finished."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.finished = False

    async def render(self, ctx: HookContext) -> str:
        await asyncio.sleep(0.05)
        self.finished = True
        return f"{self.name} done"


class BlockingHook:
    """Blocks until cancelled; its cleanup takes `cleanup_s`, then notes it finished."""

    def __init__(self, name: str, started: asyncio.Event, cleanup_s: float) -> None:
        self.name = name
        self.started = started
        self.cleanup_s = cleanup_s
        self.finished = False

    async def render(self, ctx: HookContext) -> str:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(self.cleanup_s)
            self.finished = True
        return "never"


async def test_a_hook_cancelling_itself_gives_one_line_and_its_siblings_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slow = SlowHook("slow")
    registry = _registry(RaisingHook("stop", asyncio.CancelledError()), slow)
    with caplog.at_level(logging.ERROR):
        text = await registry.run(["stop", "slow"], CTX)
    assert text == (
        '<data source="hook:stop">\n[stop: unavailable (cancelled)]\n</data>\n\n'
        '<data source="hook:slow">\nslow done\n</data>'
    )
    assert slow.finished
    failure = next(r for r in caplog.records if "stop" in r.getMessage())
    assert failure.exc_info is not None


async def test_a_hook_cancel_text_is_scrubbed() -> None:
    error = asyncio.CancelledError("gave up on https://user:s3cret@example.com/x")
    text = await _registry(RaisingHook("stop", error)).run(["stop"], CTX)
    assert "s3cret" not in text
    assert text.startswith('<data source="hook:stop">\n[stop: unavailable (')


async def test_cancelling_the_run_propagates_and_no_hook_outlives_it() -> None:
    first_started, second_started = asyncio.Event(), asyncio.Event()
    # The first ends its cleanup at once, the second later: the run must wait for both.
    first = BlockingHook("first", first_started, cleanup_s=0)
    second = BlockingHook("second", second_started, cleanup_s=0.1)
    run = asyncio.create_task(_registry(first, second).run(["first", "second"], CTX))
    await asyncio.wait_for(first_started.wait(), timeout=5)
    await asyncio.wait_for(second_started.wait(), timeout=5)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert first.finished
    assert second.finished
