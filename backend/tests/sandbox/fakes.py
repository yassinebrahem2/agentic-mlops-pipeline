from pathlib import Path

from mlops_agents.errors import DomainError


class MemorySandboxProjects:
    def __init__(self, roots: dict[str, Path] | None = None) -> None:
        self.roots = roots or {}
        self.network: dict[str, bool] = {}

    async def root(self, project_id: str) -> Path:
        if project_id not in self.roots:
            raise DomainError("project_not_found", "This project does not exist.", status=404)
        return self.roots[project_id]

    async def get_network(self, project_id: str) -> bool:
        return self.network.get(project_id, False)

    async def set_network(self, project_id: str, enabled: bool) -> None:
        self.network[project_id] = enabled
