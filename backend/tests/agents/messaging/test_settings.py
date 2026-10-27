from __future__ import annotations

import pytest
from pydantic import ValidationError

from mlops_agents.agents.messaging.settings import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_TOKENS,
    MessagingLimits,
    MessagingSettings,
)
from tests.agents.messaging.bus_env import BusEnv


async def test_the_limits_default_to_six_hops_and_300000_tokens(bus_env: BusEnv) -> None:
    limits = await bus_env.settings.get(bus_env.session.project_id)

    assert (limits.max_hops, limits.max_tokens) == (6, 300_000)
    assert (DEFAULT_MAX_HOPS, DEFAULT_MAX_TOKENS) == (6, 300_000)


async def test_the_limits_round_trip(bus_env: BusEnv) -> None:
    settings: MessagingSettings = bus_env.settings
    project_id = bus_env.session.project_id

    await settings.set(project_id, MessagingLimits(max_hops=3, max_tokens=5000))
    again = await settings.set(project_id, MessagingLimits(max_hops=4, max_tokens=6000))

    assert again == await settings.get(project_id)
    assert (again.max_hops, again.max_tokens) == (4, 6000)


@pytest.mark.parametrize("hops, tokens", [(0, 10), (1, 0), (-1, 10)])
def test_a_limit_below_one_is_invalid(hops: int, tokens: int) -> None:
    with pytest.raises(ValidationError):
        MessagingLimits(max_hops=hops, max_tokens=tokens)
