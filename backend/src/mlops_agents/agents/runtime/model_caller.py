"""Model caller with streaming, retries, key rotation, and usage reporting."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage

from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.errors import ProviderErrorKind, classify
from mlops_agents.providers.service import ProvidersService


@dataclass(frozen=True)
class ModelReply:
    message: AIMessage  # text and tool calls, tool-call ids as the provider sent them
    prompt_tokens: int | None
    completion_tokens: int | None
    tool_call_indices: dict[str, int] = field(default_factory=dict[str, int])


class TurnFailed(Exception):  # message is safe to show and is stored as the turn's error
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _OnTextCallbackError(Exception):
    """Wrapper so on_text exceptions are not caught and classified as provider errors."""


def _extract_text_deltas(content: object) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    deltas: list[str] = []
    if isinstance(content, list):
        parts = cast(list[object], content)
        for part in parts:
            if isinstance(part, str) and part:
                deltas.append(part)
            elif isinstance(part, dict):
                part_dict = cast(dict[object, object], part)
                if part_dict.get("type") == "text":
                    text = str(part_dict.get("text", ""))
                    if text:
                        deltas.append(text)
    return deltas


MAX_ATTEMPTS = 6
TRANSIENT_BACKOFFS = (1.0, 2.0)
MAX_REJECTED_RETRIES = 2


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
        call_messages = list(messages)
        key_index = 0
        rate_limited_keys: set[int] = set()
        transient_retries = 0
        rejected_retries = 0
        attempts = 0
        text_sent = False

        while True:
            total_chunk: AIMessageChunk | None = None
            last_chunk: AIMessageChunk | None = None

            try:
                client = model.build(self._http, key_index)
                runnable = (
                    client.bind_tools(tools)  # pyright: ignore[reportUnknownMemberType]  # LangChain ChatOpenAI.bind_tools has unknown kwargs
                    if tools
                    else client
                )
                async for chunk in runnable.astream(call_messages):
                    aim_chunk = cast(AIMessageChunk, chunk)
                    last_chunk = aim_chunk
                    total_chunk = aim_chunk if total_chunk is None else total_chunk + aim_chunk

                    for delta in _extract_text_deltas(aim_chunk.content):
                        text_sent = True
                        try:
                            res = on_text(delta)
                            if inspect.isawaitable(res):
                                await res
                        except Exception as cb_exc:
                            raise _OnTextCallbackError() from cb_exc

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

                tool_call_indices: dict[str, int] = {}
                if total_chunk is not None and total_chunk.tool_call_chunks:
                    for tc_chunk in total_chunk.tool_call_chunks:
                        cid = tc_chunk.get("id")
                        idx = tc_chunk.get("index")
                        if cid and idx is not None:
                            tool_call_indices[str(cid)] = int(idx)

                return ModelReply(
                    message=final_message,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    tool_call_indices=tool_call_indices,
                )

            except _OnTextCallbackError as wrapper:
                if wrapper.__cause__ is not None:
                    raise wrapper.__cause__ from None
                raise

            except Exception as exc:
                error = classify(exc, secrets=model.keys)

                if text_sent:
                    raise TurnFailed(error.message) from exc

                if error.kind is ProviderErrorKind.BAD_GENERATION:
                    if rejected_retries < MAX_REJECTED_RETRIES:
                        rejected_retries += 1
                        corrective = (
                            f"Your last tool call was rejected by the provider: {error.message}. "
                            "Call the tool again with every required argument, or answer without it."
                        )
                        call_messages.append(HumanMessage(content=corrective))
                        continue
                    raise TurnFailed(error.message) from exc

                attempts += 1

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
