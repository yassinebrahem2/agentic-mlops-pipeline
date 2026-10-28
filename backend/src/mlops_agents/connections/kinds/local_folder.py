"""Local folder connection kind."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from mlops_agents.connections.contract import (
    LOCAL_FOLDER_KIND,
    DuckDbSource,
    Findings,
    LocalFolderSettings,
    Mount,
    TableRef,
)
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.connections.kinds.local_folder_scan import (
    compute_data_version,
    latest_data_version,
    scan_tables_and_unsupported,
    walk_findings,
)
from mlops_agents.errors import DomainError
from mlops_agents.workspace.fs import user_path


class LocalFolderKind:
    """Connection kind for local disk folders."""

    name: str = LOCAL_FOLDER_KIND
    settings_model: type[BaseModel] = LocalFolderSettings
    needs_credential: bool = False

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir.resolve()

    def _resolve(self, settings: BaseModel) -> Path:
        text = str(getattr(settings, "path", "")).strip()

        if "," in text or '"' in text:
            raise DomainError(
                "connection_invalid",
                "Path cannot contain comma or quotation mark.",
                status=400,
            )

        if os.name != "nt" and ":" in text:
            raise DomainError(
                "connection_invalid",
                "POSIX path cannot contain colon.",
                status=400,
            )

        if text.startswith(("\\\\", "//")):
            raise DomainError(
                "connection_invalid",
                "Network and UNC paths are not supported.",
                status=400,
            )

        path = user_path(text)
        if not path.is_absolute():
            raise DomainError(
                "connection_invalid",
                "Give the full path of the folder.",
                status=400,
            )

        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            raise DomainError(
                "connection_invalid",
                f"Folder does not exist: {text}",
                status=400,
            ) from None
        except PermissionError:
            raise DomainError(
                "connection_invalid",
                f"The app is not allowed to read {text}.",
                status=400,
            ) from None
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            raise DomainError(
                "connection_invalid",
                f"{text} could not be read ({reason}).",
                status=400,
            ) from None

        if not resolved.is_dir():
            raise DomainError(
                "connection_invalid",
                f"Path is not a directory: {text}",
                status=400,
            )

        if resolved.drive.startswith(("\\\\", "//")) or str(resolved).startswith(("\\\\", "//")):
            raise DomainError(
                "connection_invalid",
                "Network and UNC paths are not supported.",
                status=400,
            )

        if (
            "," in str(resolved)
            or '"' in str(resolved)
            or (os.name != "nt" and ":" in str(resolved))
        ):
            raise DomainError(
                "connection_invalid",
                "Path contains unsupported characters.",
                status=400,
            )

        try:
            with os.scandir(resolved):
                pass
        except PermissionError:
            raise DomainError(
                "connection_invalid",
                f"The app is not allowed to read {text}.",
                status=400,
            ) from None
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            raise DomainError(
                "connection_invalid",
                f"{text} could not be read ({reason}).",
                status=400,
            ) from None

        data = self._data_dir
        if resolved == data:
            raise DomainError(
                "connection_invalid",
                f"{text} is the application data folder.",
                status=400,
            )
        if resolved.is_relative_to(data):
            raise DomainError(
                "connection_invalid",
                f"{text} is inside the application data folder.",
                status=400,
            )
        if data.is_relative_to(resolved):
            raise DomainError(
                "connection_invalid",
                f"{text} contains the application data folder.",
                status=400,
            )

        return resolved

    def validate(self, settings: BaseModel, credential: str | None) -> None:
        del credential
        self._resolve(settings)

    def findings(self, settings: BaseModel) -> Findings:
        resolved = self._resolve(settings)
        _, unsupported = scan_tables_and_unsupported(resolved)
        return walk_findings(resolved, unsupported)

    def tables(self, settings: BaseModel) -> list[TableRef]:
        resolved = self._resolve(settings)
        tables, _ = scan_tables_and_unsupported(resolved)
        return tables

    def data_version(self, settings: BaseModel, table: TableRef) -> str:
        resolved = self._resolve(settings)
        return compute_data_version(resolved, table)

    def latest_data_version(self, settings: BaseModel, tables: list[TableRef]) -> str | None:
        resolved = self._resolve(settings)
        return latest_data_version(resolved, tables)

    def duckdb_source(self, settings: BaseModel, table: TableRef) -> DuckDbSource:
        resolved = self._resolve(settings)

        def _escape_sql_literal(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        paths_str = (
            "["
            + ", ".join(_escape_sql_literal((resolved / f).as_posix()) for f in table.files)
            + "]"
        )
        multi = len(table.files) > 1

        if table.format == "csv":
            sql_from = (
                f"read_csv({paths_str}, union_by_name=true)" if multi else f"read_csv({paths_str})"
            )
        elif table.format == "parquet":
            sql_from = (
                f"read_parquet({paths_str}, union_by_name=true)"
                if multi
                else f"read_parquet({paths_str})"
            )
        elif table.format == "json":
            sql_from = (
                f"read_json({paths_str}, format='auto', union_by_name=true)"
                if multi
                else f"read_json({paths_str}, format='auto')"
            )
        elif table.format == "jsonl":
            sql_from = (
                f"read_json({paths_str}, format='newline_delimited', union_by_name=true)"
                if multi
                else f"read_json({paths_str}, format='newline_delimited')"
            )
        else:
            raise DomainError(
                "connection_invalid",
                f"Unsupported format '{table.format}'.",
                status=400,
            )

        return DuckDbSource(sql_from=sql_from, allowed_directories=[resolved])

    def sandbox_mount(self, name: str, settings: BaseModel) -> Mount | None:
        resolved = self._resolve(settings)
        return Mount(host_path=resolved, container_path=f"{DATA_MOUNT_ROOT}/{name}")
