"""Data connections (I15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

ConnectionStatus = Literal["ok", "unreachable"]

# The one source of the connection-name rule.
CONNECTION_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,39}$"

LocalFolderKindName = Literal["local_folder"]
LOCAL_FOLDER_KIND: LocalFolderKindName = "local_folder"


class LocalFolderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class TableRef(BaseModel):
    name: str
    files: list[str]
    format: Literal["csv", "parquet", "json", "jsonl"]


class ColumnInfo(BaseModel):
    name: str
    type: str


class TableDetail(BaseModel):
    """Served by data-summary from its cache."""

    table: TableRef
    columns: list[ColumnInfo]
    row_count: int | None  # None: not computed (a timed-out profile)
    timed_out: bool
    data_version: str
    summary: str


class Mount(BaseModel):
    """Always mounted read-only."""

    host_path: Path
    container_path: str


class DuckDbSource(BaseModel):
    sql_from: str
    allowed_directories: list[Path]


class Finding(BaseModel):
    path: str
    reason: str


class Findings(BaseModel):
    unsupported_files: list[Finding]
    secret_files: list[Finding]
    truncated: bool


class ConnectionView(BaseModel):
    id: str
    name: str
    kind: str
    settings: dict[str, Any]
    status: ConnectionStatus
    status_detail: str | None
    findings: Findings
    # The data version of the table whose newest file has the latest modified time.
    data_version: str | None


class ConnectionKind(Protocol):
    name: str
    settings_model: type[BaseModel]
    needs_credential: bool

    def validate(self, settings: BaseModel, credential: str | None) -> None:
        """Raises with a user-facing reason."""
        ...

    def findings(self, settings: BaseModel) -> Findings: ...

    def tables(self, settings: BaseModel) -> list[TableRef]: ...

    def data_version(self, settings: BaseModel, table: TableRef) -> str: ...

    def read(self, settings: BaseModel) -> tuple[Findings, str | None]:
        """One folder check and one scan: the findings and ConnectionView.data_version; None
        when the kind cannot tell cheaply."""
        ...

    def duckdb_source(self, settings: BaseModel, table: TableRef) -> DuckDbSource: ...

    def sandbox_mount(self, name: str, settings: BaseModel) -> Mount | None:
        """Mounted at /data/<name> in the container."""
        ...


class Connections(Protocol):
    async def inspect(self, project_id: str, kind: str, settings: dict[str, Any]) -> Findings:
        """Validates and reports; creates nothing."""
        ...

    async def create(
        self, project_id: str, kind: str, name: str, settings: dict[str, Any]
    ) -> ConnectionView: ...

    async def update(
        self, project_id: str, connection_id: str, settings: dict[str, Any]
    ) -> ConnectionView: ...

    async def delete(self, project_id: str, connection_id: str) -> None: ...

    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]: ...

    async def data_version(self, project_id: str, connection_id: str, table: str) -> str: ...

    async def duckdb_source(
        self, project_id: str, connection_id: str, table: str
    ) -> DuckDbSource: ...

    async def mounts(self, project_id: str) -> list[Mount]: ...

    # Last: once defined, the name list would shadow the builtin in later annotations.
    async def list(self, project_id: str) -> list[ConnectionView]: ...
