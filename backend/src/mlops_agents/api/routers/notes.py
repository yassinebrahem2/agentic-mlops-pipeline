"""Project knowledge notes routes."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.knowledge.contract import NoteDraft, NoteType, NoteView, SearchResult

router = APIRouter(tags=["notes"])


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: NoteType | None = None
    title: str | None = None
    text: str | None = None
    source: str | None = None


@router.get(
    "/projects/{project_id}/notes",
    response_model=SearchResult,
)
async def list_notes(
    project: ProjectDep,
    services: ServicesDep,
    q: str = "",
    type: NoteType | None = None,
    limit: int = 20,
) -> SearchResult:
    return await services.knowledge.search(project.id, query=q, type=type, limit=limit)


@router.post(
    "/projects/{project_id}/notes",
    status_code=status.HTTP_201_CREATED,
    response_model=NoteView,
)
async def create_note(
    project: ProjectDep,
    services: ServicesDep,
    body: NoteDraft,
) -> NoteView:
    return await services.knowledge.save(project.id, author="user", note=body)


@router.patch(
    "/projects/{project_id}/notes/{note_id}",
    response_model=NoteView,
)
async def patch_note(
    project: ProjectDep,
    note_id: str,
    body: NoteUpdate,
    services: ServicesDep,
) -> NoteView:
    if not body.model_dump(exclude_unset=True):
        raise DomainError(
            "empty_patch",
            "At least one field must be provided to update a note",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if not note_id.startswith("kno_"):
        raise DomainError(
            "note_not_found",
            f"Note {note_id} not found",
            status=status.HTTP_404_NOT_FOUND,
        )
    existing = await services.knowledge.get(project.id, note_id)
    if existing is None:
        raise DomainError(
            "note_not_found",
            f"Note {note_id} not found",
            status=status.HTTP_404_NOT_FOUND,
        )
    draft = NoteDraft(
        type=body.type or existing.type,
        title=body.title if body.title is not None else existing.title,
        text=body.text if body.text is not None else existing.text,
        source=body.source if body.source is not None else existing.source,
    )
    return await services.knowledge.save(
        project.id,
        author=existing.author,
        note=draft,
        note_id=note_id,
    )


@router.delete(
    "/projects/{project_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_note(
    project: ProjectDep,
    note_id: str,
    services: ServicesDep,
) -> Response:
    if not note_id.startswith("kno_"):
        raise DomainError(
            "note_not_found",
            f"Note {note_id} not found",
            status=status.HTTP_404_NOT_FOUND,
        )
    await services.knowledge.delete(project.id, note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
