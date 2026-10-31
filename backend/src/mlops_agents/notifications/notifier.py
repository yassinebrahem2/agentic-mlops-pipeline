"""SqliteNotifier implementing Notifier (I28)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from ulid import ULID

from mlops_agents.config import RunMode
from mlops_agents.credentials.scrub import error_text, scrub_values
from mlops_agents.events.contract import EventLog, EventType
from mlops_agents.monitoring.incidents.contract import Incidents, SignalDraft, cause_key
from mlops_agents.notifications.constants import (
    ERROR_MAX_CHARS,
    FAILURE_SIGNAL_AFTER,
    RETRY_BACKOFFS_S,
)
from mlops_agents.notifications.contract import (
    Channel,
    Message,
    NotificationKind,
    Notifier,
    OnCallPerson,
)
from mlops_agents.notifications.links import incident_link
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.proposals.payloads import Contact
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log, on_call_people


class SqliteNotifier(Notifier):
    """The central notifier delivering notifications across Discord and Email channels."""

    def __init__(
        self,
        databases: Databases,
        settings: IntegrationSettings,
        channels: Mapping[str, Channel] | Sequence[Channel],
        events: EventLog,
        clock: Clock,
        run_mode: RunMode = RunMode.LOCAL,
    ) -> None:
        self._databases = databases
        self._settings = settings
        if isinstance(channels, Sequence):
            self._channels: dict[str, Channel] = {ch.name: ch for ch in channels}
        else:
            self._channels = dict(channels)
        self._events = events
        self._clock = clock
        self._run_mode = run_mode
        self._incidents: Incidents | None = None

    def bind_incidents(self, incidents: Incidents) -> None:
        """Bind incidents service for channel failure signals (task 4)."""
        self._incidents = incidents

    async def _check_channel_failure(
        self,
        project_id: str,
        ch_name: str,
        incident_id: str | None,
        last_error: str | None,
    ) -> None:
        """Raises a notification_failure signal after FAILURE_SIGNAL_AFTER consecutive delivery failures."""
        if self._incidents is None:
            return

        # S4 §8, §13: A failure while delivering that incident's own notifications raises nothing more.
        if incident_id is not None:
            inc = await self._incidents.get(project_id, incident_id)
            if inc is not None and inc.cause_key == cause_key(f"channel:{ch_name}", None):
                return

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = (
                select(notification_log.c.status, notification_log.c.last_error)
                .where(
                    notification_log.c.channel == ch_name,
                    notification_log.c.status.in_(["sent", "failed"]),
                )
                .order_by(notification_log.c.created.desc(), notification_log.c.id.desc())
                .limit(10)
            )
            rows = (await conn.execute(stmt)).all()

        consecutive = 0
        error_msg = last_error or ""
        for r in rows:
            if r.status == "failed":
                consecutive += 1
                if not error_msg and r.last_error:
                    error_msg = r.last_error
            else:
                break

        if consecutive >= FAILURE_SIGNAL_AFTER:
            now = self._clock.now()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            now_utc = now.astimezone(UTC)
            draft = SignalDraft(
                kind="notification_failure",
                category="operational",
                subject=f"channel:{ch_name}",
                model=None,
                ran_at=now_utc,
                detail={
                    "channel": ch_name,
                    "error": error_msg,
                    "consecutive_failures": consecutive,
                },
            )
            await self._incidents.raise_signal(project_id, draft, now_utc)

    def _event_type_for_sent(self, kind: NotificationKind) -> EventType:
        if kind in ("incident", "promotion"):
            return "notification.sent"
        if kind == "escalation":
            return "escalation.sent"
        if kind == "report":
            return "report.sent"
        return "notification.sent"

    async def _save_person_contacts(self, project_id: str, person: OnCallPerson) -> None:
        """Saves or updates person in on_call_people so retries can resolve contact addresses."""
        engine = await self._databases.project(project_id)
        contacts_json = json.dumps([c.model_dump() for c in person.contacts])
        hours_json = json.dumps(person.hours.model_dump())
        async with engine.begin() as conn:
            stmt = (
                sqlite_insert(on_call_people)
                .values(
                    id=person.id,
                    name=person.name,
                    contacts=contacts_json,
                    hours=hours_json,
                    timezone=person.timezone,
                    escalation_order=person.escalation_order,
                )
                .on_conflict_do_update(
                    index_elements=[on_call_people.c.id],
                    set_={
                        "name": person.name,
                        "contacts": contacts_json,
                        "hours": hours_json,
                        "timezone": person.timezone,
                        "escalation_order": person.escalation_order,
                    },
                )
            )
            await conn.execute(stmt)

    async def _get_contact_address(
        self, project_id: str, person_id: str, channel_name: str
    ) -> str | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            raw = await conn.scalar(
                select(on_call_people.c.contacts).where(on_call_people.c.id == person_id)
            )
        if not raw:
            return None
        try:
            contacts_data = json.loads(str(raw))
            for item in contacts_data:
                c = Contact.model_validate(item)
                if c.channel == channel_name:
                    return c.address
        except Exception:
            return None
        return None

    async def notify(
        self,
        project_id: str,
        person: OnCallPerson,
        message: Message,
        *,
        kind: NotificationKind,
        incident_id: str | None,
    ) -> Literal["sent", "retrying", "failed"]:
        await self._save_person_contacts(project_id, person)

        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_iso = now.isoformat()

        public_url = await self._settings.public_url()
        if incident_id is not None:
            link = incident_link(public_url, project_id, incident_id)
        elif not public_url:
            link = None
        else:
            link = message.link
        local_mode_note = self._run_mode == RunMode.LOCAL
        message = message.model_copy(update={"link": link, "local_mode_note": local_mode_note})

        # Calculate escalation_step
        escalation_step = 0
        if kind != "incident" and incident_id:
            engine = await self._databases.project(project_id)
            async with engine.connect() as conn:
                stmt = select(func.count(func.distinct(notification_log.c.person))).where(
                    notification_log.c.incident == incident_id,
                    notification_log.c.kind.in_(["incident", "escalation"]),
                )
                escalation_step = int((await conn.scalar(stmt)) or 0)

        discord_st = await self._settings.discord()
        email_st = await self._settings.email()
        secrets = await self._settings.secret_values()

        results: list[Literal["sent", "retrying", "failed"]] = []
        message_json = message.model_dump_json()

        engine = await self._databases.project(project_id)

        for contact in person.contacts:
            ch_name = contact.channel
            channel = self._channels.get(ch_name)

            # Check if channel is enabled and set up
            is_enabled_and_setup = False
            fail_reason = ""
            if ch_name == "discord":
                if not discord_st.enabled:
                    fail_reason = "Discord channel is disabled."
                elif not discord_st.webhook_set:
                    fail_reason = "Discord webhook is not configured."
                else:
                    is_enabled_and_setup = True
            elif ch_name == "email":
                if not email_st.enabled:
                    fail_reason = "Email channel is disabled."
                elif not email_st.host:
                    fail_reason = "Email host is not configured."
                else:
                    is_enabled_and_setup = True
            else:
                fail_reason = f"Unknown channel {ch_name}."

            if not is_enabled_and_setup or channel is None:
                # Log failed immediately without attempting delivery
                row_id = str(ULID())
                async with engine.begin() as conn:
                    await conn.execute(
                        notification_log.insert().values(
                            id=row_id,
                            incident=incident_id,
                            person=person.id,
                            channel=ch_name,
                            kind=kind,
                            escalation_step=escalation_step,
                            status="failed",
                            attempts=0,
                            next_attempt=None,
                            last_error=fail_reason,
                            message=message_json,
                            created=now_iso,
                            sent=None,
                        )
                    )
                await self._events.record(
                    project_id,
                    source="system",
                    type="notification.failed",
                    payload={
                        "person": person.id,
                        "channel": ch_name,
                        "kind": kind,
                        "attempt": 0,
                    },
                    subject_id=incident_id,
                )
                await self._check_channel_failure(project_id, ch_name, incident_id, fail_reason)
                results.append("failed")
                continue

            # Channel is enabled and set up: attempt delivery inline
            attempts = 1
            row_id = str(ULID())
            try:
                await channel.send(contact.address, message)
                # Success
                async with engine.begin() as conn:
                    await conn.execute(
                        notification_log.insert().values(
                            id=row_id,
                            incident=incident_id,
                            person=person.id,
                            channel=ch_name,
                            kind=kind,
                            escalation_step=escalation_step,
                            status="sent",
                            attempts=attempts,
                            next_attempt=None,
                            last_error=None,
                            message=message_json,
                            created=now_iso,
                            sent=now_iso,
                        )
                    )
                evt_type = self._event_type_for_sent(kind)
                await self._events.record(
                    project_id,
                    source="system",
                    type=evt_type,
                    payload={
                        "person": person.id,
                        "channel": ch_name,
                        "kind": kind,
                        "attempt": attempts,
                    },
                    subject_id=incident_id,
                )
                results.append("sent")
            except Exception as exc:
                # Inline attempt failed
                to_scrub = [*list(secrets), contact.address]
                scrubbed_err = scrub_values(error_text(exc), to_scrub)[:ERROR_MAX_CHARS]
                next_backoff: int = int(RETRY_BACKOFFS_S[0])
                next_attempt_dt = now + timedelta(seconds=next_backoff)
                next_attempt_iso = next_attempt_dt.isoformat()

                async with engine.begin() as conn:
                    await conn.execute(
                        notification_log.insert().values(
                            id=row_id,
                            incident=incident_id,
                            person=person.id,
                            channel=ch_name,
                            kind=kind,
                            escalation_step=escalation_step,
                            status="retrying",
                            attempts=attempts,
                            next_attempt=next_attempt_iso,
                            last_error=scrubbed_err,
                            message=message_json,
                            created=now_iso,
                            sent=None,
                        )
                    )
                results.append("retrying")

        if any(r == "sent" for r in results):
            return "sent"
        if any(r == "retrying" for r in results):
            return "retrying"
        return "failed"

    async def retry_pending(self, project_id: str, now: datetime) -> None:
        """Processes all pending retrying rows for project_id whose next_attempt <= now."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_iso = now.isoformat()

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = (
                select(notification_log)
                .where(
                    notification_log.c.status == "retrying",
                    notification_log.c.next_attempt <= now_iso,
                )
                .order_by(notification_log.c.created)
            )
            rows = (await conn.execute(stmt)).mappings().all()

        if not rows:
            return

        discord_st = await self._settings.discord()
        email_st = await self._settings.email()
        secrets = await self._settings.secret_values()

        for row in rows:
            row_id = row["id"]
            person_id = row["person"]
            ch_name = row["channel"]
            kind: NotificationKind = row["kind"]
            incident_id = row["incident"]
            attempts: int = int(row["attempts"]) + 1

            address = await self._get_contact_address(project_id, person_id, ch_name)
            channel = self._channels.get(ch_name)

            # Check if channel is still enabled and set up
            is_enabled_and_setup = False
            fail_reason = ""
            if ch_name == "discord":
                if not discord_st.enabled:
                    fail_reason = "Discord channel is disabled."
                elif not discord_st.webhook_set:
                    fail_reason = "Discord webhook is not configured."
                else:
                    is_enabled_and_setup = True
            elif ch_name == "email":
                if not email_st.enabled:
                    fail_reason = "Email channel is disabled."
                elif not email_st.host:
                    fail_reason = "Email host is not configured."
                else:
                    is_enabled_and_setup = True

            if not is_enabled_and_setup or channel is None or address is None:
                err_text = fail_reason or "Contact address or channel not available."
                async with engine.begin() as conn:
                    await conn.execute(
                        update(notification_log)
                        .where(notification_log.c.id == row_id)
                        .values(
                            status="failed",
                            attempts=attempts,
                            next_attempt=None,
                            last_error=err_text,
                        )
                    )
                await self._events.record(
                    project_id,
                    source="system",
                    type="notification.failed",
                    payload={
                        "person": person_id,
                        "channel": ch_name,
                        "kind": kind,
                        "attempt": attempts,
                    },
                    subject_id=incident_id,
                )
                await self._check_channel_failure(project_id, ch_name, incident_id, err_text)
                continue

            try:
                message = Message.model_validate_json(row["message"])
            except Exception:
                async with engine.begin() as conn:
                    await conn.execute(
                        update(notification_log)
                        .where(notification_log.c.id == row_id)
                        .values(
                            status="failed",
                            next_attempt=None,
                            last_error="Corrupt message payload.",
                        )
                    )
                await self._events.record(
                    project_id,
                    source="system",
                    type="notification.failed",
                    payload={
                        "person": person_id,
                        "channel": ch_name,
                        "kind": kind,
                        "attempt": attempts,
                    },
                    subject_id=incident_id,
                )
                await self._check_channel_failure(
                    project_id, ch_name, incident_id, "Corrupt message payload."
                )
                continue

            try:
                await channel.send(address, message)
                # Success on retry!
                async with engine.begin() as conn:
                    await conn.execute(
                        update(notification_log)
                        .where(notification_log.c.id == row_id)
                        .values(
                            status="sent",
                            attempts=attempts,
                            next_attempt=None,
                            last_error=None,
                            sent=now_iso,
                        )
                    )
                evt_type = self._event_type_for_sent(kind)
                await self._events.record(
                    project_id,
                    source="system",
                    type=evt_type,
                    payload={
                        "person": person_id,
                        "channel": ch_name,
                        "kind": kind,
                        "attempt": attempts,
                    },
                    subject_id=incident_id,
                )
            except Exception as exc:
                to_scrub = [*list(secrets), address]
                scrubbed_err = scrub_values(error_text(exc), to_scrub)[:ERROR_MAX_CHARS]
                # Check backoffs: attempts index
                backoff_idx: int = attempts - 1
                if backoff_idx < len(RETRY_BACKOFFS_S):
                    backoff = RETRY_BACKOFFS_S[backoff_idx]
                    next_attempt_dt = now + timedelta(seconds=backoff)
                    async with engine.begin() as conn:
                        await conn.execute(
                            update(notification_log)
                            .where(notification_log.c.id == row_id)
                            .values(
                                status="retrying",
                                attempts=attempts,
                                next_attempt=next_attempt_dt.isoformat(),
                                last_error=scrubbed_err,
                            )
                        )
                else:
                    # Backoffs exhausted -> failed
                    async with engine.begin() as conn:
                        await conn.execute(
                            update(notification_log)
                            .where(notification_log.c.id == row_id)
                            .values(
                                status="failed",
                                attempts=attempts,
                                next_attempt=None,
                                last_error=scrubbed_err,
                            )
                        )
                    await self._events.record(
                        project_id,
                        source="system",
                        type="notification.failed",
                        payload={
                            "person": person_id,
                            "channel": ch_name,
                            "kind": kind,
                            "attempt": attempts,
                        },
                        subject_id=incident_id,
                    )
                    await self._check_channel_failure(
                        project_id, ch_name, incident_id, scrubbed_err
                    )
