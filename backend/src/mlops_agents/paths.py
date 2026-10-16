from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ID = re.compile(r"^[a-z]{3}_[A-Za-z0-9]+$")


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def app_db(self) -> Path:
        return self.root / "app.db"

    @property
    def keys_file(self) -> Path:
        return self.root / "keys.enc"

    @property
    def cookie_secret(self) -> Path:
        return self.root / "cookie_secret"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    def project_dir(self, project_id: str) -> Path:
        if not _PROJECT_ID.match(project_id):
            raise ValueError(f"project_id must match ^[a-z]{{3}}_[A-Za-z0-9]+$, got {project_id!r}")
        return self.projects / project_id

    def project_db(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.db"

    def repo_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "repo"

    def ensure(self) -> None:
        for folder in (self.root, self.backups, self.projects, self.bin):
            folder.mkdir(parents=True, exist_ok=True)
