"""Unit tests for embedding key, input formatting, and ReembedPass (I17)."""

from __future__ import annotations

import asyncio
from datetime import datetime

from mlops_agents.knowledge.contract import NoteDraft, NoteView
from mlops_agents.knowledge.embedding import ReembedPass, embedder_key, embedding_input
from mlops_agents.knowledge.limits import EMBED_INPUT_CHARS, RETRY_AFTER_SECONDS


def test_embedder_key_format() -> None:
    assert embedder_key("text-embedding-3-small", 1536) == "text-embedding-3-small@1536"
    assert embedder_key("groq/llama-embedding", 768) == "groq/llama-embedding@768"
    assert embedder_key("local/custom", 128) == "local/custom@128"


def test_embedding_input_draft() -> None:
    draft = NoteDraft(
        type="fact",
        title="Title Here",
        text="Text body here.",
    )
    assert embedding_input(draft) == "Title Here\n\nText body here."


def test_embedding_input_view() -> None:
    view = NoteView(
        id="kno_123",
        type="runbook",
        title="Incident Runbook",
        text="Step 1: Check logs.",
        source="wiki",
        author="alice",
        created=datetime.now(),
        updated=datetime.now(),
    )
    assert embedding_input(view) == "Incident Runbook\n\nStep 1: Check logs."


def test_embedding_input_truncates_at_limit() -> None:
    title = "A" * 100
    text = "B" * 10000
    draft = NoteDraft(type="doc", title=title, text=text)
    inp = embedding_input(draft)
    assert len(inp) == EMBED_INPUT_CHARS
    assert inp.startswith("A" * 100 + "\n\n")


def test_reembed_pass_initial_state() -> None:
    p = ReembedPass()
    assert p.task is None
    assert not p.running
    assert not p.rerun
    assert p.last_failure_time is None
    assert p.last_failure_msg is None
    assert p.should_retry(now=0.0)


def test_reembed_pass_failure_and_cooldown() -> None:
    p = ReembedPass()
    p.record_failure("Rate limited", now=100.0)
    assert p.last_failure_msg == "Rate limited"
    assert p.last_failure_time == 100.0

    # Within cooldown
    assert not p.should_retry(now=100.0 + RETRY_AFTER_SECONDS - 1.0)
    # At or after cooldown
    assert p.should_retry(now=100.0 + RETRY_AFTER_SECONDS)
    assert p.should_retry(now=100.0 + RETRY_AFTER_SECONDS + 10.0)

    # Clear failure
    p.clear_failure()
    assert p.last_failure_msg is None
    assert p.last_failure_time is None
    assert p.should_retry(now=100.0)


async def test_reembed_pass_running_property() -> None:
    p = ReembedPass()
    assert not p.running

    async def dummy() -> None:
        await asyncio.sleep(0.05)

    task = asyncio.create_task(dummy())
    p.task = task
    assert p.running
    await task
    assert not p.running
