from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.providers.bridge import ResolvedModel


class MockSummaryCaller(ModelCaller):
    def __init__(self, summary_text: str = "Summary of earlier turns") -> None:
        self.summary_text = summary_text
        self.calls: list[dict[str, Any]] = []

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
    ) -> ModelReply:
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        return ModelReply(
            message=AIMessage(content=self.summary_text),
            prompt_tokens=50,
            completion_tokens=20,
        )


def make_model(context_window: int = 1000) -> ResolvedModel:
    return ResolvedModel(
        address="fake:m",
        provider_id="fake",
        provider_name="fake",
        base_url="http://fake",
        wire_model_id="m",
        context_window=context_window,
        keys=(),
    )
