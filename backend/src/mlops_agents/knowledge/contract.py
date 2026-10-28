"""Project knowledge notes (I17)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

NoteType = Literal["fact", "decision", "incident", "runbook", "doc"]


class NoteDraft(BaseModel):
    type: NoteType
    title: str
    text: str
    source: str | None = None


class NoteView(BaseModel):
    id: str
    type: NoteType
    title: str
    text: str
    source: str | None
    author: str
    created: datetime
    updated: datetime


class SearchResult(BaseModel):
    notes: list[NoteView]
    used_vectors: bool
    detail: str | None  # why vectors were not used


class VectorStore(Protocol):
    async def upsert(
        self, project_id: str, note_id: str, vector: list[float], embedder: str
    ) -> None: ...

    async def remove(self, project_id: str, note_id: str) -> None: ...

    async def nearest(
        self, project_id: str, vector: list[float], embedder: str, k: int
    ) -> list[str]: ...

    async def drop_embedder(self, project_id: str, embedder: str) -> None: ...


class Knowledge(Protocol):
    async def save(
        self, project_id: str, author: str, note: NoteDraft, note_id: str | None = None
    ) -> NoteView: ...

    async def get(self, project_id: str, note_id: str) -> NoteView | None: ...

    async def search(
        self, project_id: str, query: str, type: NoteType | None, limit: int
    ) -> SearchResult: ...

    async def delete(self, project_id: str, note_id: str) -> None: ...
