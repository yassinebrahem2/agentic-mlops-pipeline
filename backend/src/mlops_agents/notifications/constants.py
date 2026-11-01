"""Notifications constants (one source for notifications plan)."""

import re
from collections.abc import Sequence
from typing import Final

from mlops_agents.proposals.payloads import Contact, OnCall

SEND_TIMEOUT_S: Final[int] = 10
RETRY_BACKOFFS_S: Final[tuple[int, ...]] = (30, 120, 600)
FAILURE_SIGNAL_AFTER: Final[int] = 3
DISCORD_CONTENT_MAX: Final[int] = 2000
ERROR_MAX_CHARS: Final[int] = 500
INCIDENT_PATH: Final[str] = "/projects/{project_id}/incidents/{incident_id}"
INBOX_PATH: Final[str] = "/projects/{project_id}/inbox"
ON_CALL_SEEDED_KEY: Final[str] = "on_call_handoff_version"

# Deliberately small: a typo catcher (missing "@" or domain dot), not a deliverability check.
EMAIL_PATTERN: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def contact_problem(contact: Contact) -> str | None:
    """What is wrong with a contact's address, or None. The one rule the on-call route, the
    handoff seeder and the handoff card's warnings share."""
    # The raw address is what is stored and sent, so padding is refused, not trimmed.
    address = contact.address
    if contact.channel == "email":
        return None if EMAIL_PATTERN.fullmatch(address) else "is not a valid email address"
    if not address.strip():
        return "has an empty Discord user ID"
    return None if address == address.strip() else "has spaces around the Discord user ID"


def contact_problems(people: Sequence[OnCall]) -> list[str]:
    """One sentence per mistyped contact, naming the person and channel (never the address)."""
    return [
        f"{person.name}'s {contact.channel} contact {problem}."
        for person in people
        for contact in person.contacts
        if (problem := contact_problem(contact)) is not None
    ]
