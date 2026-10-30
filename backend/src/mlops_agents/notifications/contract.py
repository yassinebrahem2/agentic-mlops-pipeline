"""Notifications, on-call and morning report contracts and protocols (I28)."""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.incidents.contract import Severity
from mlops_agents.proposals.payloads import OnCall

DISCORD_WEBHOOK_REF: Final = "app/discord_webhook"
EMAIL_PASSWORD_REF: Final = "app/email_password"  # noqa: S105 -- keystore key reference, not a credential
PUBLIC_URL_KEY: Final = "public_url"
DISCORD_SETTINGS_KEY: Final = "discord_settings"
EMAIL_SETTINGS_KEY: Final = "email_settings"


class DiscordSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    webhook_set: bool


class EmailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    host: str
    port: int
    user: str
    sender: str
    starttls: bool
    password_set: bool


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    severity: Severity
    cause: str | None = None
    done: list[str] = []
    waiting: list[str] = []
    lines: list[str] = []
    link: str | None = None
    local_mode_note: bool = False
    urgent: bool = False
    diagnosis_pending: bool = False


class ChannelError(Exception):
    """User-facing scrubbed channel error."""


class Channel(Protocol):
    name: Literal["discord", "email"]

    async def send(self, target: str, message: Message) -> None: ...


class OnCallPerson(OnCall):
    id: str


class OnCallPeople(Protocol):
    async def list(self, project_id: str) -> list[OnCallPerson]: ...

    async def replace(self, project_id: str, people: list[OnCall]) -> list[OnCallPerson]: ...


NotificationKind = Literal["incident", "escalation", "report", "promotion"]


class Notifier(Protocol):
    async def notify(
        self,
        project_id: str,
        person: OnCallPerson,
        message: Message,
        *,
        kind: NotificationKind,
        incident_id: str | None,
    ) -> Literal["sent", "retrying", "failed"]: ...


class ReportAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str | None = None
    action: str
    why: str
    basis: Literal["rule", "policy", "approval", "tier1", "bypass"]
    basis_ref: str | None = None


class MorningReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str
    since: datetime
    incidents: list[str]
    actions: list[ReportAction]
    waiting: list[str]
    suspended: list[ExpectationKey]
    resolved_offline: list[str]
    wakes_skipped: list[str]


class MorningReports(Protocol):
    async def build(
        self, project_id: str, person_id: str, since: datetime
    ) -> MorningReportBody: ...
