"""A real provider accepts the Propose tool's schema as the app sends it (acceptance, run once by
hand with `uv run pytest -m live`; it does not gate CI)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import httpx2
import pytest

from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME
from mlops_agents.api.services import Services
from mlops_agents.providers.env_keys import KNOWN_ENV_KEYS
from tests.api.conftest import started_app

pytestmark = pytest.mark.live

LIVE_PROVIDERS = ("groq", "gemini")


def _refs(value: object) -> list[str]:
    """Every `$ref` left anywhere in a schema."""
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in cast(dict[str, object], value).items():
            found += [str(item)] if key == "$ref" else _refs(item)
        return found
    if isinstance(value, list):
        return [ref for item in cast(list[object], value) for ref in _refs(item)]
    return []


async def test_a_real_provider_accepts_the_propose_schema(tmp_path: Path) -> None:
    # The schema exactly as ModelCaller binds it: the registry's, after _clean_schema.
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        [tool] = services.tools.schemas([PROPOSE_TOOL_NAME])
    assert _refs(tool) == []

    live_model = os.environ.get("MLOPS_AGENTS_LIVE_MODEL")
    if not live_model:
        pytest.skip("set MLOPS_AGENTS_LIVE_MODEL (groq:<model> or gemini:<model>) to run this")
    provider, _, wire_id = live_model.partition(":")
    if provider not in LIVE_PROVIDERS or not wire_id:
        pytest.fail(
            f"MLOPS_AGENTS_LIVE_MODEL must be '<provider>:<model>' with one of {LIVE_PROVIDERS}"
        )
    [(key_env, base_url)] = [
        (env, url) for env, (name, url) in KNOWN_ENV_KEYS.items() if name == provider
    ]
    key = os.environ.get(key_env)
    if not key:
        pytest.skip(f"set {key_env} to run this live test")

    async with httpx2.AsyncClient(timeout=60.0) as http:
        response = await http.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": wire_id,
                "messages": [{"role": "user", "content": "Say hello. Do not call any tool."}],
                "tools": [tool],
            },
        )

    # A rejected tool definition is a 400; the reply's own text is not checked.
    # The body as text: an error page need not be JSON.
    assert response.status_code == 200, f"{response.status_code}: {response.text[:2000]}"
