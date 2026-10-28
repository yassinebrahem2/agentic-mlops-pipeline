"""Key failover: a successful call clears its provider's needs-attention mark, through
`on_success` (Decision #384), and the app wires it to `ProvidersService.clear_attention`."""

import logging
from pathlib import Path

import pytest

from mlops_agents.api.services import Services
from mlops_agents.providers.failover import AllKeysExhausted, KeyFailover
from mlops_agents.providers.models import Provider, ProviderCreate
from tests.api.conftest import started_app
from tests.providers.fake_clock import (
    FakeClock,
    Script,
    bad_request,
    provider,
    rate_limit,
    unauthorized,
)

COOLDOWN = 15
MODEL = "llama-3.3-70b"


def needing_attention(key_count: int) -> Provider:
    return provider(key_count).model_copy(update={"status": "needs_attention"})


class Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[Provider] = []
        self._error = error

    async def __call__(self, called: Provider) -> None:
        self.calls.append(called)
        if self._error is not None:
            raise self._error


async def test_a_success_reports_a_provider_needing_attention_once() -> None:
    recorder = Recorder()
    failover = KeyFailover(FakeClock(), on_success=recorder)
    marked = needing_attention(2)

    result = await failover.call(
        marked, MODEL, Script(rate_limit(), "reply"), cooldown_minutes=COOLDOWN
    )

    assert result == "reply"
    assert recorder.calls == [marked]


async def test_a_success_on_a_healthy_provider_reports_nothing() -> None:
    recorder = Recorder()
    failover = KeyFailover(FakeClock(), on_success=recorder)

    await failover.call(provider(2), MODEL, Script("reply"), cooldown_minutes=COOLDOWN)

    assert recorder.calls == []


async def test_a_keyless_success_reports_a_provider_needing_attention() -> None:
    recorder = Recorder()
    failover = KeyFailover(FakeClock(), on_success=recorder)
    marked = needing_attention(0)

    assert await failover.call(marked, MODEL, Script("reply"), cooldown_minutes=COOLDOWN) == (
        "reply"
    )

    assert recorder.calls == [marked]


async def test_a_failing_on_success_is_logged_and_the_call_still_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failover = KeyFailover(FakeClock(), on_success=Recorder(RuntimeError("database locked")))

    with caplog.at_level(logging.ERROR, logger="mlops_agents.providers.failover"):
        result = await failover.call(
            needing_attention(1), MODEL, Script("reply"), cooldown_minutes=COOLDOWN
        )

    assert result == "reply"
    [record] = [r for r in caplog.records if r.exc_info is not None]
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


@pytest.mark.parametrize(
    ("key_count", "outcomes", "raised"),
    [
        (1, (unauthorized(),), AllKeysExhausted),
        (1, (bad_request(),), Exception),
        (0, (unauthorized(),), Exception),
    ],
    ids=["every-key-exhausted", "re-raised", "keyless"],
)
async def test_a_call_that_fails_reports_nothing(
    key_count: int, outcomes: tuple[BaseException, ...], raised: type[BaseException]
) -> None:
    recorder = Recorder()
    failover = KeyFailover(FakeClock(), on_success=recorder)

    with pytest.raises(raised):
        await failover.call(
            needing_attention(key_count), MODEL, Script(*outcomes), cooldown_minutes=COOLDOWN
        )

    assert recorder.calls == []


async def test_the_apps_failover_clears_the_mark_on_success(tmp_path: Path) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        created = await services.providers.create_provider(
            ProviderCreate(name="local", kind="self_hosted", base_url="http://localhost:1/v1")
        )
        await services.providers.mark_needs_attention(created.id, "HTTP 401")
        marked = await services.providers.get_provider_by_id(created.id)
        assert marked.status == "needs_attention"

        await services.failover.call(marked, MODEL, Script("reply"), cooldown_minutes=COOLDOWN)

        cleared = await services.providers.get_provider_by_id(created.id)
        assert (cleared.status, cleared.status_detail) == ("ok", None)
