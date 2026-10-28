"""The I19 field rules: time zones, on-call hours, feed refs and the connections list."""

from typing import Any

import pytest
from pydantic import ValidationError

from mlops_agents.proposals.payloads import (
    CheckSpec,
    ConnectionsPayload,
    Feed,
    HandoffPayload,
    OnCall,
    OnCallHours,
)
from tests.proposals.samples import connection, data_feed, error_text, handoff, job_feed


@pytest.mark.parametrize(
    "zone", ["Europe/Paris", "UTC", "America/Argentina/Buenos_Aires", "Etc/GMT+5"]
)
def test_valid_timezones_pass(zone: str) -> None:
    on_call = handoff()["operations"]["on_call"][0]
    assert OnCall.model_validate({**on_call, "timezone": zone}).timezone == zone


# The odd names are ones a Windows file lookup would forgive (trailing space or dot, backslash);
# posix/..., right/... and posixrules are in Linux's system database but not in tzdata's list.
@pytest.mark.parametrize(
    "zone",
    [
        "Mars/Base",
        "",
        "../etc/passwd",
        "Paris",
        "Europe",
        "zone.tab",
        "Europe/Paris ",
        "Europe/Paris.",
        "Europe\\Paris",
        "Europe/Paris\n",
        "posix/Europe/Paris",
        "right/Europe/Paris",
        "posixrules",
    ],
)
def test_invalid_timezones_fail_on_every_carrier(zone: str) -> None:
    ops = handoff()["operations"]
    with pytest.raises(ValidationError):
        OnCall.model_validate({**ops["on_call"][0], "timezone": zone})
    with pytest.raises(ValidationError):
        CheckSpec.model_validate({**ops["checks"][0], "timezone": zone})
    with pytest.raises(ValidationError):
        HandoffPayload.model_validate(
            handoff(operations={**ops, "feeds": [{**ops["feeds"][0], "timezone": zone}]})
        )


def test_the_timezone_error_names_the_zone() -> None:
    on_call = handoff()["operations"]["on_call"][0]
    with pytest.raises(ValidationError) as err:
        OnCall.model_validate({**on_call, "timezone": "Mars/Base"})
    assert "Mars/Base" in error_text(err)


@pytest.mark.parametrize(("start", "end"), [("09:00", "17:00"), ("22:00", "06:00")])
def test_on_call_hours_allow_a_window_over_midnight(start: str, end: str) -> None:
    OnCallHours.model_validate({"days": ["mon"], "start": start, "end": end})


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("09:00", "09:00"),
        ("9:00", "17:00"),
        ("09:00", "24:00"),
        ("09:60", "17:00"),
        ("", "17:00"),
        # Arabic-indic digits: \d would accept them.
        ("0\N{ARABIC-INDIC DIGIT NINE}:00", "17:00"),
        ("09:00", "17:0\N{ARABIC-INDIC DIGIT FIVE}"),
    ],
)
def test_on_call_hours_reject_equal_or_malformed(start: str, end: str) -> None:
    with pytest.raises(ValidationError):
        OnCallHours.model_validate({"days": ["mon"], "start": start, "end": end})


def test_on_call_hours_need_a_day() -> None:
    with pytest.raises(ValidationError):
        OnCallHours.model_validate({"days": [], "start": "09:00", "end": "17:00"})


def test_feed_kind_matches_its_ref() -> None:
    Feed.model_validate(data_feed())
    Feed.model_validate(job_feed())
    job_ref: dict[str, Any] = job_feed()["ref"]
    with pytest.raises(ValidationError) as err:
        Feed.model_validate(data_feed(ref=job_ref))
    assert "bookings" in error_text(err)
    assert "DataFeedRef" in error_text(err)
    with pytest.raises(ValidationError) as err2:
        Feed.model_validate(job_feed(ref=data_feed()["ref"]))
    assert "nightly" in error_text(err2)
    assert "JobFeedRef" in error_text(err2)


def test_connections_payload_needs_a_connection() -> None:
    with pytest.raises(ValidationError):
        ConnectionsPayload.model_validate({"connections": []})


def test_connection_names_are_unique() -> None:
    ConnectionsPayload.model_validate({"connections": [connection("a"), connection("b")]})
    with pytest.raises(ValidationError) as err:
        ConnectionsPayload.model_validate({"connections": [connection("a"), connection("a")]})
    assert "'a'" in error_text(err)
