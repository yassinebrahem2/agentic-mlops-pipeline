from __future__ import annotations

import httpx2
import pytest

from benchmarks.stub_model import start_stub_model


@pytest.mark.asyncio
async def test_stub_model_server() -> None:
    stub = start_stub_model("stub-test-model")
    try:
        assert "127.0.0.1" in stub.base_url

        async with httpx2.AsyncClient(base_url=stub.base_url) as client:
            # 1. Models endpoint
            resp_models = await client.get("/models")
            assert resp_models.status_code == 200
            data_models = resp_models.json()
            assert any(m["id"] == "stub-test-model" for m in data_models["data"])

            # 2. Chat completion non-streaming
            resp_chat = await client.post(
                "/chat/completions",
                json={
                    "model": "stub-test-model",
                    "messages": [{"role": "user", "content": "Investigate incident"}],
                    "stream": False,
                },
            )
            assert resp_chat.status_code == 200
            chat_data = resp_chat.json()
            assert "choices" in chat_data
            choice = chat_data["choices"][0]
            assert choice["finish_reason"] == "stop"
            msg = choice["message"]
            assert msg["role"] == "assistant"
            assert msg.get("tool_calls") is None or len(msg.get("tool_calls", [])) == 0
            assert isinstance(msg["content"], str) and len(msg["content"]) > 0

            # 3. Chat completion streaming
            async with client.stream(
                "POST",
                "/chat/completions",
                json={
                    "model": "stub-test-model",
                    "messages": [{"role": "user", "content": "Investigate incident"}],
                    "stream": True,
                },
            ) as stream_resp:
                assert stream_resp.status_code == 200
                content = await stream_resp.aread()
                text = content.decode("utf-8")
                assert "data: " in text
                assert "[DONE]" in text
                assert "tool_calls" not in text
    finally:
        stub.stop()
