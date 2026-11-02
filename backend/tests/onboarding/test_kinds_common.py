"""Parts: each part's outcome recorded, every reason stored without a secret."""

from __future__ import annotations

import logging

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.onboarding.kinds_common import UNEXPECTED_REASON, Parts

SECRET = "sk-live-Qw7Zp3Lm9"


async def _secrets() -> list[str]:
    return [SECRET]


async def _secrets_down() -> list[str]:
    raise DomainError("gh_failed", "gh could not be run")


async def test_a_reason_holding_a_secret_is_stored_masked() -> None:
    parts = Parts(_secrets)

    async def leaks() -> None:
        raise DomainError("refused", f"the tracker refused the key {SECRET}")

    assert not await parts.run("tracking:0", leaks)

    reason = parts.result().failed["tracking:0"]
    assert SECRET not in reason
    assert reason == "the tracker refused the key ***"


async def test_an_unexpected_error_stores_the_generic_reason_and_logs_without_raw_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    parts = Parts(_secrets)

    async def breaks() -> None:
        raise KeyError("boom")

    with caplog.at_level(logging.ERROR, logger="mlops_agents.onboarding.kinds_common"):
        assert not await parts.run("network", breaks)

    assert parts.result().failed == {"network": UNEXPECTED_REASON}
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "network" in record.getMessage()
    assert "boom" in record.getMessage()
    assert record.exc_info is None


async def test_an_apply_failing_with_credential_leaves_it_out_of_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    parts = Parts(_secrets)

    async def leaks() -> None:
        raise RuntimeError(f"connection to postgres://user:{SECRET}@db failed with key {SECRET}")

    with caplog.at_level(logging.ERROR, logger="mlops_agents.onboarding.kinds_common"):
        assert not await parts.run("network", leaks)

    assert parts.result().failed == {"network": UNEXPECTED_REASON}
    assert SECRET not in caplog.text
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "network" in record.getMessage()
    assert "***" in record.getMessage()
    assert record.exc_info is None


async def test_a_failing_secret_source_keeps_the_reason_url_scrubbed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    parts = Parts(_secrets_down)

    async def missing_table() -> None:
        raise DomainError("handoff_references", "the connection hotel has no table t")

    with caplog.at_level(logging.WARNING, logger="mlops_agents.onboarding.kinds_common"):
        await parts.run("handoff", missing_table)
        await parts.fail("kickoff", "not started: the handoff was not stored")
        await parts.fail("tracking:0", "http://u:pw@mlflow:5000/?token=abc refused")

    failed = parts.result().failed
    assert failed["handoff"] == "the connection hotel has no table t"
    assert failed["kickoff"] == "not started: the handoff was not stored"
    assert "pw" not in failed["tracking:0"] and "abc" not in failed["tracking:0"]
    assert any("handoff" in r.getMessage() for r in caplog.records)


async def test_an_applied_part_is_listed_unless_unlisted() -> None:
    parts = Parts(_secrets)

    async def ok() -> None:
        return None

    assert await parts.run("level", ok)
    assert await parts.run("credentials", ok, listed=False)

    assert parts.result().applied == ["level"]
    assert parts.result().failed == {}
