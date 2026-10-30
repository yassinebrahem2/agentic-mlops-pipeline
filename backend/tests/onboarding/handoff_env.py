"""The handoff kind over real services: connections on a folder with three tables, tracking against
the fake MLflow server, a fake sandbox and a recording (or real) kickoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx2
from sqlalchemy import insert

from mlops_agents.clock import now_iso
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.onboarding.handoff_kind import HandoffKind
from mlops_agents.onboarding.handoff_parts import KickoffStarter
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.tracking.adapters.mlflow import MlflowAdapter
from mlops_agents.tracking.contract import (
    Page,
    SourceType,
    Tracking,
    TrackingQuery,
    TrackingSourceView,
)
from mlops_agents.tracking.service import TrackingService
from tests.conftest import MemoryKeyStore
from tests.proposals.samples import command, data_feed, handoff, job_feed, model
from tests.tracking.conftest import FakeTrackingAdapter

PROPOSAL = "prp_handoff"
TABLES = ("predictions", "labels", "bookings")


def payload(mlflow_url: str, **overrides: Any) -> HandoffPayload:
    """The sample handoff, its tracking sources pointed at the fake MLflow server."""
    tracking = [
        {"type": "mlflow", "settings": {"url": mlflow_url}, "auth": "none"},
        {
            "type": "github_actions",
            "settings": {"repository": "acme/ml", "workflows": ["train.yml"]},
            "auth": "none",
        },
    ]
    return HandoffPayload.model_validate(handoff(**{"tracking": tracking, **overrides}))


# A handoff part (or operations sub-part) that names what the project lacks, and the reason.
UNRESOLVED_REFERENCES: list[tuple[dict[str, Any], str]] = [
    (
        {
            "data_roles": [
                {
                    "connection": "nowhere",
                    "table": "t",
                    "role": "other",
                    "id_column": None,
                    "time_column": None,
                }
            ],
            "models": [],
        },
        "needs the connection nowhere first",
    ),
    (
        {
            "data_roles": [
                {
                    "connection": "hotel",
                    "table": "missing",
                    "role": "other",
                    "id_column": None,
                    "time_column": None,
                }
            ],
            "models": [],
        },
        "the connection hotel has no table missing",
    ),
    (
        {"feeds": [data_feed(ref={"connection": "nowhere", "table": "bookings"})]},
        "needs the connection nowhere first",
    ),
    (
        {"feeds": [data_feed(ref={"connection": "hotel", "table": "missing"})]},
        "the connection hotel has no table missing",
    ),
    (
        {
            "feeds": [
                job_feed(
                    ref={
                        "ci_workflow": None,
                        "file": {"connection": "nowhere", "path_pattern": "*.json"},
                    }
                )
            ]
        },
        "needs the connection nowhere first",
    ),
    (
        {"results_location": {"connection": "nowhere", "path_pattern": "r/*.json"}},
        "needs the connection nowhere first",
    ),
    (
        {
            "checks": [
                {
                    "check_id": "chk",
                    "name": "Rows",
                    "category": "data_quality",
                    "cadence": {"cron": "0 6 * * *", "every_minutes": None},
                    "timezone": "UTC",
                    "results": {"connection": "nowhere", "path_pattern": "dq/*.json"},
                }
            ]
        },
        "needs the connection nowhere first",
    ),
]


def full_payload(mlflow_url: str) -> HandoffPayload:
    """Two models (a team gate with environments; a person with a file production signal) and
    a project-level hold command, over the sample's feeds, on call, checks and results."""
    hold = command(
        kind="hold",
        template="python hold.py --until {window_end}",
        placeholders=["window_end"],
        entry_files=["hold.py"],
    )
    noshow = model(
        "noshow",
        promotion="person",
        environments={},
        production_signal={"kind": "file", "ref": "results/production.json"},
    )
    base = handoff()
    return payload(mlflow_url, models=[model("cancel"), noshow], commands=[*base["commands"], hold])


@dataclass
class FakeSandbox:
    """The network setting of SandboxService; `down` makes `set_network` fail as without Docker."""

    network: bool = False
    down: bool = False
    set_calls: list[bool] = field(default_factory=list[bool])

    async def get_network(self, project_id: str) -> bool:
        return self.network

    async def set_network(self, project_id: str, enabled: bool) -> bool:
        self.set_calls.append(enabled)
        if self.down:
            raise SandboxUnavailable("Docker is down")
        self.network = enabled
        return enabled


@dataclass
class RecordingKickoff:
    calls: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])

    async def start(
        self, project_id: str, version: int, proposal_id: str, payload: HandoffPayload
    ) -> list[str]:
        self.calls.append((project_id, version, proposal_id))
        return []


class FailingTracking:
    """The real tracking service, except that creating a source with `fail_url` raises."""

    def __init__(self, inner: Tracking, fail_url: str, reason: str) -> None:
        self._inner = inner
        self._fail_url = fail_url
        self.reason = reason

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        return await self._inner.sources(project_id)

    async def create_source(
        self,
        project_id: str,
        type: SourceType,
        settings: dict[str, Any],
        auth: Literal["none", "credential"],
    ) -> TrackingSourceView:
        if settings.get("url") == self._fail_url:
            raise DomainError("tracking_refused", self.reason, status=422)
        return await self._inner.create_source(project_id, type, settings, auth)

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        await self._inner.sync(project_id, source_id)

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        return await self._inner.query(project_id, q)


@dataclass
class HandoffEnv:
    project_id: str
    databases: Databases
    keystore: MemoryKeyStore
    secrets: ProjectSecrets
    events: SqliteEventLog
    connections: ConnectionsService
    tracking: TrackingService
    sandbox: FakeSandbox
    handoffs: Handoffs
    kickoff: KickoffStarter
    http: httpx2.AsyncClient

    def kind(
        self, *, tracking: Tracking | None = None, handoffs: Handoffs | None = None
    ) -> HandoffKind:
        return HandoffKind(
            databases=self.databases,
            connections=self.connections,
            tracking=tracking or self.tracking,
            handoffs=handoffs or self.handoffs,
            sandbox=self.sandbox,
            events=self.events,
            kickoff=self.kickoff,
            secrets=self.secrets,
            secret_values=_no_secrets,
        )

    async def close(self) -> None:
        await self.tracking.close()
        await self.http.aclose()


async def _no_secrets() -> list[str]:
    return []


async def add_project(databases: Databases, project_id: str) -> None:
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name="Hotel",
                source="github",
                github_full_name="acme/hotel",
                github_key="acme/hotel",
                default_branch="main",
                status="ok",
                created_at=now_iso(),
            )
        )


async def build_handoff_env(
    *,
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    project_id: str,
    kickoff: KickoffStarter,
) -> HandoffEnv:
    """The project already exists; a `hotel` connection holds the sample's three tables."""
    events = SqliteEventLog(databases)
    connections = ConnectionsService(databases, events, KindRegistry([LocalFolderKind(paths.root)]))
    folder = tmp_path / "hotel"
    folder.mkdir()
    for table in TABLES:
        (folder / f"{table}.csv").write_text("booking_id,value\n1,2\n", encoding="utf-8")
    await connections.create(project_id, "local_folder", "hotel", {"path": str(folder)})
    secrets = ProjectSecrets(keystore, databases)
    http = httpx2.AsyncClient()

    async def no_token() -> str | None:
        return None

    tracking = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters={
            "mlflow": MlflowAdapter(http=http),
            "github_actions": FakeTrackingAdapter("github_actions"),
        },
        github_token=no_token,
        handoffs=SqliteHandoffs(databases),
    )
    return HandoffEnv(
        project_id=project_id,
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        connections=connections,
        tracking=tracking,
        sandbox=FakeSandbox(),
        handoffs=SqliteHandoffs(databases),
        kickoff=kickoff,
        http=http,
    )
