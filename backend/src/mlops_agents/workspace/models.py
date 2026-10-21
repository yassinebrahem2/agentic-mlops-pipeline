from typing import Literal

from pydantic import BaseModel

ProjectSource = Literal["local", "github"]
ProjectStatus = Literal["ok", "missing", "empty", "clone_failed"]


class Project(BaseModel):
    id: str
    name: str
    source: ProjectSource
    local_path: str | None
    github_full_name: str | None
    default_branch: str | None
    status: ProjectStatus
    status_detail: str | None
    created_at: str
    last_opened_at: str | None


class FolderEntry(BaseModel):
    name: str
    path: str
    is_git_repo: bool


class FolderListing(BaseModel):
    path: str
    parent: str | None
    entries: list[FolderEntry]
    roots: list[str]
