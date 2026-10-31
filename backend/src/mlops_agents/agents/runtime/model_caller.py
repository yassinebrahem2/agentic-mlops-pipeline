"""Model caller: streaming, usage reporting and the corrective retry, on the app's one
`KeyFailover`, which moves between the provider's keys."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, cast

import httpx2
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage

from mlops_agents.errors import DomainError
from mlops_agents.providers.bridge import ResolvedModel, deduplicate_repeated_strings
from mlops_agents.providers.errors import ProviderErrorKind, classify
from mlops_agents.providers.failover import (
    AllKeysExhausted,
    KeyFailover,
    KeyState,
    no_usable_key,
)
from mlops_agents.providers.models import Provider
from mlops_agents.providers.service import ProvidersService

# No key in these states works until someone fixes it, so the provider needs attention.
_NEEDS_FIXING = ("billing", "rejected")

MAX_REJECTED_RETRIES = 2


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


class ModelKeysExhausted(Exception):
    """Every key of the provider is exhausted for the model. Never a turn's failure: the turn
    graph catches it only to stall the turn (stall.py), which Try again resumes."""

    def __init__(
        self, *, provider: Provider, model: str, keys: list[KeyState], description: str
    ) -> None:
        headline = no_usable_key(provider.name, model)
        super().__init__(f"{headline}. {description}")
        self.headline = headline
        self.provider_id = provider.id
        self.model = model
        self.keys = keys
        self.description = description


class _OnTextCallbackError(Exception):
    """Wrapper so on_text and on_restart exceptions are not caught and classified as provider
    errors."""


class _StaleKeys(Exception):
    """The failover picked a key the call's read of the keys does not have: they changed."""

    def __init__(self) -> None:
        super().__init__("The provider's keys changed during the model call.")


class _Rejected(Exception):
    """The provider rejected the generation (a malformed tool call): worth a corrective retry."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


def _reply(total_chunk: AIMessageChunk | None, last_chunk: AIMessageChunk | None) -> ModelReply:
    usage: dict[str, Any] | None = None
    if total_chunk is not None and hasattr(total_chunk, "usage_metadata"):
        usage = cast(dict[str, Any] | None, total_chunk.usage_metadata)
    if usage is None and last_chunk is not None and hasattr(last_chunk, "usage_metadata"):
        usage = cast(dict[str, Any] | None, last_chunk.usage_metadata)

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    if usage is not None:
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")

    tool_call_indices: dict[str, int] = {}
    if total_chunk is not None and total_chunk.tool_call_chunks:
        for tc_chunk in total_chunk.tool_call_chunks:
            cid = tc_chunk.get("id")
            idx = tc_chunk.get("index")
            if cid and idx is not None:
                tool_call_indices[str(cid)] = int(idx)

    additional_kwargs: dict[str, Any] = {}
    if total_chunk is not None:
        additional_kwargs = dict(total_chunk.additional_kwargs)
        extra_by_idx = additional_kwargs.get("tool_calls_extra_content", {})
        model_by_idx = additional_kwargs.get("tool_calls_model", {})
        if extra_by_idx and tool_call_indices:
            extra_by_id: dict[str, Any] = dict(extra_by_idx)
            model_by_id: dict[str, str] = dict(model_by_idx)
            for cid, idx in tool_call_indices.items():
                idx_str = str(idx)
                if idx_str in extra_by_idx:
                    extra_by_id[cid] = extra_by_idx[idx_str]
                if idx_str in model_by_idx:
                    model_by_id[cid] = model_by_idx[idx_str]
            additional_kwargs["tool_calls_extra_content"] = extra_by_id
            if model_by_id:
                additional_kwargs["tool_calls_model"] = model_by_id

        if "tool_calls_extra_content" in additional_kwargs:
            additional_kwargs["tool_calls_extra_content"] = deduplicate_repeated_strings(
                additional_kwargs["tool_calls_extra_content"]
            )
        if "tool_calls_model" in additional_kwargs:
            additional_kwargs["tool_calls_model"] = deduplicate_repeated_strings(
                additional_kwargs["tool_calls_model"]
            )

    if total_chunk is None:
        final_message = AIMessage(content="")
    else:
        final_message = AIMessage(
            content=total_chunk.content,
            tool_calls=total_chunk.tool_calls,
            invalid_tool_calls=total_chunk.invalid_tool_calls,
            response_metadata=total_chunk.response_metadata,
            id=total_chunk.id,
            additional_kwargs=additional_kwargs,
        )

    return ModelReply(
        message=final_message,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        tool_call_indices=tool_call_indices,
    )


@dataclass
class _Call:
    """One failover call: the provider and keys it read."""

    model: ResolvedModel  # with the keys read for this call
    provider: Provider
    secrets: tuple[str, ...] = field(repr=False)  # key values: never in a repr


class _Stream:
    """The text of one `call`, across its attempts, its stale-keys restart and its corrective
    retries. Each attempt starts by taking back the text the previous one streamed
    (`on_restart`, Decision #410): only a next attempt takes it back, so a failure with no next
    attempt keeps its text, and text taken back never returns."""

    def __init__(
        self,
        on_text: Callable[[str], Awaitable[None]],
        on_restart: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._on_text = on_text
        self._on_restart = on_restart
        self._sent = False

    async def text(self, delta: str) -> None:
        self._sent = True
        try:
            res = self._on_text(delta)
            if inspect.isawaitable(res):
                await res
        except Exception as cb_exc:
            raise _OnTextCallbackError() from cb_exc

    async def take_back(self) -> None:
        """At the start of an attempt: take back the text the previous attempt streamed, if
        any."""
        if not self._sent:
            return
        self._sent = False
        if self._on_restart is None:
            return
        try:
            await self._on_restart()
        except Exception as cb_exc:
            raise _OnTextCallbackError() from cb_exc


class ModelCaller:
    def __init__(
        self, providers: ProvidersService, http: httpx2.AsyncClient, failover: KeyFailover
    ) -> None:
        self._providers = providers
        self._http = http
        self._failover = failover

    async def call(
        self,
        model: ResolvedModel,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], Awaitable[None]],
        on_restart: Callable[[], Awaitable[None]] | None = None,
    ) -> ModelReply:
        """`on_restart` takes back, when the next attempt starts, the text a failed attempt
        streamed; None (a call whose text is not shown) takes nothing back."""
        stream = _Stream(on_text, on_restart)
        call_messages = list(messages)
        rejected_retries = 0
        while True:
            try:
                return await self._call_restarting(model, call_messages, tools, stream)
            except _Rejected as rejected:
                if rejected_retries >= MAX_REJECTED_RETRIES:
                    raise TurnFailed(rejected.message) from rejected.__cause__
                rejected_retries += 1
                corrective = (
                    f"Your last tool call was rejected by the provider: {rejected.message}. "
                    "Call the tool again with every required argument, or answer without it."
                )
                call_messages.append(HumanMessage(content=corrective))

    async def reset_keys(self, provider_id: str) -> None:
        """Try again: forget the provider's key states, so the next call tries every key again,
        and its needs-attention mark. A provider deleted since the stall has nothing to reset:
        its deletion cleared both, and the turn goes on with the agent's model."""
        try:
            provider = await self._providers.get_provider_by_id(provider_id)
        except DomainError as error:
            if error.code == "provider_not_found":
                return
            raise
        self._failover.clear(provider)
        await self._providers.clear_attention(provider_id)

    async def _call_restarting(
        self,
        model: ResolvedModel,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        stream: _Stream,
    ) -> ModelReply:
        """One failover call; restarted once with a fresh read when the keys changed under it.
        A second change escapes as `_StaleKeys`, so the turn fails as unexpected."""
        try:
            return await self._call_once(model, messages, tools, stream)
        except _StaleKeys:
            return await self._call_once(model, messages, tools, stream)

    async def _call_once(
        self,
        model: ResolvedModel,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        stream: _Stream,
    ) -> ModelReply:
        provider = await self._providers.get_provider_by_id(model.provider_id)
        keys = tuple(await self._providers.keys_for(provider.id))
        call = _Call(replace(model, keys=keys), provider, model.keys + keys)
        wire_model = model.wire_model_id

        async def attempt(key_index: int | None) -> ModelReply:
            return await self._attempt(call, key_index, messages, tools, stream)

        try:
            reply = await self._failover.call(
                provider, wire_model, attempt, cooldown_minutes=provider.cooldown_minutes
            )
        except _OnTextCallbackError as wrapper:
            if wrapper.__cause__ is not None:
                raise wrapper.__cause__ from None
            raise
        except _StaleKeys:
            raise
        except AllKeysExhausted as exhausted:
            stalled = ModelKeysExhausted(
                provider=provider,
                model=wire_model,
                keys=exhausted.keys,
                description=self._failover.describe_keys(exhausted.keys),
            )
            await self._flag_if_unfixable(provider, exhausted.keys, str(stalled))
            raise stalled from exhausted
        except Exception as exc:
            error = classify(exc, secrets=call.secrets)
            if error.kind is ProviderErrorKind.BAD_GENERATION:
                raise _Rejected(error.message) from exc
            raise TurnFailed(error.message) from exc
        return reply

    async def _attempt(
        self,
        call: _Call,
        key_index: int | None,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        stream: _Stream,
    ) -> ModelReply:
        """One request with one key (None: the keyless provider), started by taking back the
        text a previous attempt streamed. It raises the provider's raw exception."""
        await stream.take_back()
        keys = call.model.keys
        if (key_index is None and keys) or (key_index is not None and key_index >= len(keys)):
            raise _StaleKeys()
        client = call.model.build(self._http, key_index or 0)
        runnable = (
            client.bind_tools(tools)  # pyright: ignore[reportUnknownMemberType]  # LangChain ChatOpenAI.bind_tools has unknown kwargs
            if tools
            else client
        )
        total_chunk: AIMessageChunk | None = None
        last_chunk: AIMessageChunk | None = None
        async for chunk in runnable.astream(messages):
            aim_chunk = cast(AIMessageChunk, chunk)
            last_chunk = aim_chunk
            total_chunk = aim_chunk if total_chunk is None else total_chunk + aim_chunk
            for delta in _extract_text_deltas(aim_chunk.content):
                await stream.text(delta)
        return _reply(total_chunk, last_chunk)

    async def _flag_if_unfixable(
        self, provider: Provider, states: list[KeyState], message: str
    ) -> None:
        if states and all(state.status in _NEEDS_FIXING for state in states):
            await self._providers.mark_needs_attention(provider.id, message)
