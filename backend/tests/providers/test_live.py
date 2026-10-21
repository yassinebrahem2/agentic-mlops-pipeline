import os

import httpx2
import pytest

from mlops_agents.providers.verification import probe_model

pytestmark = pytest.mark.live


async def test_groq_streams_tool_calls() -> None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        pytest.skip("set GROQ_API_KEY to run this live test")
    async with httpx2.AsyncClient() as http:
        result = await probe_model(
            "https://api.groq.com/openai/v1", key, "qwen/qwen3.8-27b", http=http
        )
    assert result.tool_calling
    assert result.streaming
