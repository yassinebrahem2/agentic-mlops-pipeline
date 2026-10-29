"""Local folder connection kind."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from mlops_agents.connections.contract import (
    LOCAL_FOLDER_KIND,
    DuckDbSource,
    Finding,
    Findings,
    LocalFolderSettings,
    Mount,
    TableRef,
)
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.connections.kinds.local_folder_scan import (
    compute_data_version,
    has_wildcard,
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

        if has_wildcard(str(resolved)):
            raise DomainError(
                "connection_invalid",
                "Path cannot contain *, ? or [ (DuckDB reads them as wildcards).",
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

    def read(self, settings: BaseModel) -> tuple[Findings, str | None]:
        resolved = self._resolve(settings)
        tables, unsupported = scan_tables_and_unsupported(resolved)
        try:
            findings = self.findings(settings, resolved=resolved, unsupported=unsupported)
        except TypeError:
            findings = self.findings(settings)
        try:
            data_ver = self.latest_data_version(settings, resolved=resolved, tables=tables)
        except TypeError:
            data_ver = self.latest_data_version(settings)
        return findings, data_ver

    def findings(
        self,
        settings: BaseModel,
        resolved: Path | None = None,
        unsupported: list[Finding] | None = None,
    ) -> Findings:
        target = resolved if resolved is not None else self._resolve(settings)
        if unsupported is None:
            _, unsupported = scan_tables_and_unsupported(target)
        return walk_findings(target, unsupported)

    def tables(
        self,
        settings: BaseModel,
        resolved: Path | None = None,
    ) -> list[TableRef]:
        target = resolved if resolved is not None else self._resolve(settings)
        tables, _ = scan_tables_and_unsupported(target)
        return tables

    def data_version(
        self,
        settings: BaseModel,
        table: TableRef,
        resolved: Path | None = None,
    ) -> str:
        target = resolved if resolved is not None else self._resolve(settings)
        return compute_data_version(target, table)

    def latest_data_version(
        self,
        settings: BaseModel,
        resolved: Path | None = None,
        tables: list[TableRef] | None = None,
    ) -> str | None:
        target = resolved if resolved is not None else self._resolve(settings)
        if tables is None:
            tables, _ = scan_tables_and_unsupported(target)
        return latest_data_version(target, tables)

    def duckdb_source(
        self,
        settings: BaseModel,
        table: TableRef,
        resolved: Path | None = None,
    ) -> DuckDbSource:
        target = resolved if resolved is not None else self._resolve(settings)

        def _escape_sql_literal(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        paths_str = (
            "[" + ", ".join(_escape_sql_literal((target / f).as_posix()) for f in table.files) + "]"
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

        return DuckDbSource(sql_from=sql_from, allowed_directories=[target])

    def sandbox_mount(
        self,
        name: str,
        settings: BaseModel,
        resolved: Path | None = None,
    ) -> Mount | None:
        target = resolved if resolved is not None else self._resolve(settings)
        return Mount(host_path=target, container_path=f"{DATA_MOUNT_ROOT}/{name}")
