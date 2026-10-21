from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2
from langchain_core.messages import AIMessageChunk, HumanMessage

from mlops_agents.providers.bridge import build_chat_model
from mlops_agents.providers.errors import classify

PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_capabilities",
        "description": "Call this tool to confirm that you can call tools.",
        "parameters": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    },
}
PROBE_PROMPT = "Call the report_capabilities tool with ok set to true. Do not answer with text."


@dataclass(frozen=True)
class ProbeResult:
    tool_calling: bool
    streaming: bool
    detail: str | None


async def probe_model(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 60.0,  # noqa: ASYNC109 -- passed to the OpenAI client, which applies it per request
) -> ProbeResult:
    """One real streamed request with a tool, the way agents call models."""
    model = build_chat_model(base_url, api_key, wire_model_id, http=http, timeout=timeout)
    # langchain-openai leaves bind_tools' keyword arguments untyped.
    llm = model.bind_tools([PROBE_TOOL])  # pyright: ignore[reportUnknownMemberType]
    chunks = 0
    merged: AIMessageChunk | None = None
    try:
        async for chunk in llm.astream([HumanMessage(PROBE_PROMPT)]):
            chunks += 1
            if isinstance(chunk, AIMessageChunk):
                merged = chunk if merged is None else merged + chunk
    except Exception as exc:
        raise classify(exc, [api_key] if api_key else []) from None
    calls = merged.tool_calls if merged is not None else []
    tool_calling = any(call["name"] == "report_capabilities" for call in calls)
    streaming = chunks >= 2
    if not tool_calling:
        detail = "The model did not call the test tool. Agents need a model that calls tools."
    elif not streaming:
        detail = "The provider did not stream the answer. Agents need streaming."
    else:
        detail = None
    return ProbeResult(tool_calling, streaming, detail)
