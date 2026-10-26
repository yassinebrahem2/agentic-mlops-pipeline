from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any, cast

from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog, EventRecord, EventType, Resync


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


class FakeEventLog(EventLog):
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        rec = EventRecord(
            id=f"evt_{len(self.records) + 1}",
            seq=len(self.records) + 1,
            ts="2026-10-25T00:00:00Z",
            source=source,
            type=type,
            subject_id=subject_id,
            payload=payload,
        )
        self.records.append(rec)
        return rec

    async def _empty_stream(self) -> AsyncGenerator[EventRecord | Resync, None]:
        for _ in ():
            yield cast(EventRecord, None)

    def subscribe(
        self, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]:
        return self._empty_stream()

    async def history(
        self, project_id: str, *, types: Sequence[EventType], limit: int = 200
    ) -> list[EventRecord]:
        return [r for r in self.records if r.type in types][:limit]
