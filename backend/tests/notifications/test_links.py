from __future__ import annotations

from mlops_agents.notifications.constants import INBOX_PATH, INCIDENT_PATH
from mlops_agents.notifications.links import inbox_link, incident_link


def test_incident_link_without_public_url() -> None:
    assert incident_link(None, "prj_1", "inc_1") is None
    assert incident_link("", "prj_1", "inc_1") is None


def test_incident_link_with_public_url() -> None:
    link = incident_link("https://app.example.com", "prj_1", "inc_1")
    assert link == "https://app.example.com/projects/prj_1/incidents/inc_1"
    assert (
        link
        == f"https://app.example.com{INCIDENT_PATH.format(project_id='prj_1', incident_id='inc_1')}"
    )


def test_incident_link_strips_trailing_slash() -> None:
    link = incident_link("https://app.example.com/", "prj_1", "inc_1")
    assert link == "https://app.example.com/projects/prj_1/incidents/inc_1"


def test_inbox_link_without_public_url() -> None:
    assert inbox_link(None, "prj_1") is None
    assert inbox_link("", "prj_1") is None


def test_inbox_link_with_public_url() -> None:
    link = inbox_link("https://app.example.com", "prj_1")
    assert link == "https://app.example.com/projects/prj_1/inbox"
    assert link == f"https://app.example.com{INBOX_PATH.format(project_id='prj_1')}"


def test_inbox_link_strips_trailing_slash() -> None:
    link = inbox_link("https://app.example.com/", "prj_1")
    assert link == "https://app.example.com/projects/prj_1/inbox"
