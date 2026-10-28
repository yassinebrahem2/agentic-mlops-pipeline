"""Embedding formatting, keys, and re-embedding state (I17)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mlops_agents.knowledge.contract import NoteDraft, NoteView
from mlops_agents.knowledge.limits import EMBED_INPUT_CHARS, RETRY_AFTER_SECONDS


def embedder_key(address: str, dimensions: int) -> str:
    """Return embedder key combining address and dimensions."""
    return f"{address}@{dimensions}"


def embedding_input(note: NoteDraft | NoteView) -> str:
    """Format a note's title and text for embedding input, clamped to EMBED_INPUT_CHARS."""
    return f"{note.title}\n\n{note.text}"[:EMBED_INPUT_CHARS]


@dataclass
class ReembedPass:
    """Per-project state for background re-embedding passes."""

    task: asyncio.Task[None] | None = None
    rerun: bool = False
    last_failure_time: float | None = None
    last_failure_msg: str | None = None

    @property
    def running(self) -> bool:
        """True if the background task is running and not done."""
        return self.task is not None and not self.task.done()

    def should_retry(self, now: float, retry_after: float = RETRY_AFTER_SECONDS) -> bool:
        """True if no failure has been recorded or cooldown period has elapsed."""
        if self.last_failure_time is None:
            return True
        return (now - self.last_failure_time) >= retry_after

    def record_failure(self, message: str, now: float) -> None:
        """Record an embedder or vector store failure timestamp and message."""
        self.last_failure_msg = message
        self.last_failure_time = now

    def clear_failure(self) -> None:
        """Clear recorded failure state on recovery."""
        self.last_failure_msg = None
        self.last_failure_time = None
