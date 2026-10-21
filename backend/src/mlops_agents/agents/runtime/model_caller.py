"""Model caller with streaming, retries, key rotation, and usage reporting."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx2
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.errors import ProviderErrorKind, classify
from mlops_agents.providers.service import ProvidersService


@dataclass(frozen=True)
class ModelReply:
    message: AIMessage  # text and tool calls, tool-call ids as the provider sent them
    prompt_tokens: int | None
    completion_tokens: int | None


class TurnFailed(Exception):  # message is safe to show and is stored as the turn's error
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


MAX_ATTEMPTS = 6
TRANSIENT_BACKOFFS = (1.0, 2.0)


class ModelCaller:
    def __init__(
        self,
        providers: ProvidersService,
        http: httpx2.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._providers = providers
        self._http = http
        self._sleep = sleep

    async def call(
        self,
        model: ResolvedModel,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], Awaitable[None]],
    ) -> ModelReply:
        key_index = 0
        rate_limited_keys: set[int] = set()
        transient_retries = 0
        attempts = 0

        while True:
            attempts += 1
            streamed = False
            total_chunk: AIMessageChunk | None = None
            last_chunk: AIMessageChunk | None = None

            try:
                client = model.build(self._http, key_index)
                runnable = (
                    client.bind_tools(tools)  # pyright: ignore[reportUnknownMemberType]  # LangChain ChatOpenAI.bind_tools has unknown kwargs
                    if tools
                    else client
                )
                async for chunk in runnable.astream(messages):
                    streamed = True
                    aim_chunk = cast(AIMessageChunk, chunk)
                    last_chunk = aim_chunk
                    total_chunk = aim_chunk if total_chunk is None else total_chunk + aim_chunk

                    if isinstance(aim_chunk.content, str):
                        if aim_chunk.content:
                            res = on_text(aim_chunk.content)
                            if inspect.isawaitable(res):
                                await res
                    else:
                        for part in aim_chunk.content:
                            if isinstance(part, str) and part:
                                res = on_text(part)
                                if inspect.isawaitable(res):
                                    await res
                            elif isinstance(part, dict) and part.get("type") == "text":
                                text = str(part.get("text", ""))
                                if text:
                                    res = on_text(text)
                                    if inspect.isawaitable(res):
                                        await res

                prompt_tokens: int | None = None
                completion_tokens: int | None = None
                usage: dict[str, Any] | None = None
                if total_chunk is not None and hasattr(total_chunk, "usage_metadata"):
                    usage = cast(dict[str, Any] | None, total_chunk.usage_metadata)
                if (
                    usage is None
                    and last_chunk is not None
                    and hasattr(last_chunk, "usage_metadata")
                ):
                    usage = cast(dict[str, Any] | None, last_chunk.usage_metadata)

                if usage is not None:
                    prompt_tokens = usage.get("input_tokens")
                    completion_tokens = usage.get("output_tokens")

                if total_chunk is None:
                    final_message = AIMessage(content="")
                else:
                    final_message = AIMessage(
                        content=total_chunk.content,
                        tool_calls=total_chunk.tool_calls,
                        invalid_tool_calls=total_chunk.invalid_tool_calls,
                        response_metadata=total_chunk.response_metadata,
                        id=total_chunk.id,
                    )

                return ModelReply(
                    message=final_message,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )

            except Exception as exc:
                error = classify(exc, secrets=model.keys)

                if streamed:
                    raise TurnFailed(error.message) from exc

                if error.kind is ProviderErrorKind.RATE_LIMIT:
                    if attempts >= MAX_ATTEMPTS:
                        raise TurnFailed(error.message) from exc

                    rate_limited_keys.add(key_index)
                    num_keys = len(model.keys)
                    if num_keys <= 1 or len(rate_limited_keys) >= num_keys:
                        rate_limited_keys.clear()
                        delay = (
                            error.retry_after
                            if (error.retry_after is not None and error.retry_after > 0)
                            else 2.0
                        )
                        delay = min(delay, 30.0)
                        await self._sleep(delay)

                    if num_keys > 0:
                        key_index = (key_index + 1) % num_keys
                    continue

                if error.kind in (ProviderErrorKind.TRANSIENT, ProviderErrorKind.UNREACHABLE):
                    if attempts < MAX_ATTEMPTS and transient_retries < len(TRANSIENT_BACKOFFS):
                        delay = TRANSIENT_BACKOFFS[transient_retries]
                        transient_retries += 1
                        await self._sleep(delay)
                        continue
                    raise TurnFailed(error.message) from exc

                if error.kind in (ProviderErrorKind.AUTH, ProviderErrorKind.QUOTA):
                    await self._providers.mark_needs_attention(model.provider_id, error.message)
                    raise TurnFailed(
                        f"{model.provider_name}: {error.message}. Fix the key in AI providers."
                    ) from exc

                raise TurnFailed(error.message) from exc
