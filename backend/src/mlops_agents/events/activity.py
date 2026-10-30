"""The Activity feed: events and audit records merged by time from their two keyset reads."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated, Final, Literal

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mlops_agents.errors import DomainError
from mlops_agents.events.contract import AuditLog, AuditRecord, EventLog, EventRecord, EventType

ACTIVITY_LIMIT_DEFAULT: Final[int] = 50
ACTIVITY_LIMIT_MAX: Final[int] = 200
INCIDENT_ID_PREFIX: Final[str] = "inc_"
# Sorts after any ID, so a position (ts, _ID_CEILING) still includes every row of that ts.
_ID_CEILING: Final[str] = "\U0010ffff"


class EventEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["event"]
    ts: str
    event: EventRecord
    # Set here so the page does not carry a second copy of the ID-prefix rule.
    incident_id: str | None


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["audit"]
    ts: str
    audit: AuditRecord


ActivityEntry = Annotated[EventEntry | AuditEntry, Field(discriminator="kind")]


class ActivityPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[ActivityEntry]
    next_before: str | None  # opaque; pass it back as `before` for the next page


class _Cursor(BaseModel):
    """Where each stream stands: None means that stream has not been read yet."""

    model_config = ConfigDict(extra="forbid")
    # Bounded to what SQLite can bind (a larger value would overflow into a 500); strict so
    # `true` or `1.5` are refused instead of coerced.
    e: Annotated[int, Field(ge=0, le=2**63 - 1, strict=True)] | None = None
    a: tuple[str, str] | None = None


def _decode_cursor(raw: str | None) -> _Cursor:
    if raw is None:
        return _Cursor()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return _Cursor.model_validate_json(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, ValidationError) as exc:
        raise DomainError(
            "invalid_cursor",
            "The page cursor is not valid; reload the activity from the start.",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc


def _encode_cursor(cursor: _Cursor) -> str:
    payload = json.dumps(cursor.model_dump(mode="json"), separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _event_entry(record: EventRecord) -> EventEntry:
    subject = record.subject_id
    incident = subject if subject is not None and subject.startswith(INCIDENT_ID_PREFIX) else None
    return EventEntry(kind="event", ts=record.ts, event=record, incident_id=incident)


async def activity_page(
    event_log: EventLog,
    audit_log: AuditLog,
    project_id: str,
    *,
    type: EventType | None,
    source: str | None,
    before: str | None,
    limit: int,
) -> ActivityPage:
    """Newest first. With `type` only events are read: audit records have no event type."""
    cursor = _decode_cursor(before)
    # One extra from each stream tells whether anything is left without a further read.
    events = await event_log.page(
        project_id,
        types=[type] if type is not None else None,
        source=source,
        before=cursor.e,
        limit=limit + 1,
    )
    audits = (
        []
        if type is not None
        else await audit_log.page(project_id, actor=source, before=cursor.a, limit=limit + 1)
    )

    # Two-pointer merge keeps each stream in its own keyset order; an event wins a tie on ts.
    entries: list[ActivityEntry] = []
    ei = ai = 0
    while len(entries) <= limit and (ei < len(events) or ai < len(audits)):
        if ai >= len(audits) or (ei < len(events) and events[ei].ts >= audits[ai].ts):
            entries.append(_event_entry(events[ei]))
            ei += 1
        else:
            entries.append(AuditEntry(kind="audit", ts=audits[ai].ts, audit=audits[ai]))
            ai += 1

    has_more = len(entries) > limit
    shown = entries[:limit]
    # Advance a stream only to its last returned item, so nothing is skipped or repeated.
    next_cursor = _Cursor(e=cursor.e, a=cursor.a)
    for entry in shown:
        if isinstance(entry, EventEntry):
            next_cursor.e = entry.event.seq
        else:
            next_cursor.a = (entry.audit.ts, entry.audit.id)
    # A stream that was read but put nothing on this page must not restart from the top next
    # time: what arrives in it meanwhile is newer than this page and would show up below older
    # entries. Pin it at the newest row it had (or at nothing if it was empty).
    if not any(isinstance(e, EventEntry) for e in shown) and cursor.e is None:
        next_cursor.e = events[0].seq + 1 if events else 0
    if type is None and not any(isinstance(e, AuditEntry) for e in shown) and cursor.a is None:
        next_cursor.a = (audits[0].ts, _ID_CEILING) if audits else ("", "")
    return ActivityPage(
        entries=shown, next_before=_encode_cursor(next_cursor) if has_more else None
    )
