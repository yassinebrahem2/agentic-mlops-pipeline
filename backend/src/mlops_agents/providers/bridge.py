from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
import openai
from langchain_core.language_models.base import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import PrivateAttr, SecretStr

# Self-hosted servers usually ignore the key, but the OpenAI client refuses to start without one.
KEYLESS = "not-needed"
EMBED_BATCH = 64

# Documented by Google as a fallback placeholder for missing thought signatures:
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking/thought-signatures
SKIP_THOUGHT_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"

_current_stream_seen_ids: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "_current_stream_seen_ids", default=None
)


# Third-party boundary: LangChain OpenAI (version 1.7.2).
# Overrides ChatOpenAI._convert_chunk_to_generation_chunk, _create_chat_result, and _get_request_payload.
# LangChain drops extra_content from tool calls during streaming parse (_convert_delta_to_message_chunk)
# and request serialization (_lc_tool_call_to_openai_tool_call). Gemini 3 requires tool call extra_content
# (such as google.thought_signature) preserved and sent back on subsequent requests to the same model.
class ChatOpenAIWithExtraContent(ChatOpenAI):
    _model_address: str | None = PrivateAttr(default=None)
    _is_gemini: bool = PrivateAttr(default=False)
    _seen_call_ids: set[str] = PrivateAttr(default_factory=set)

    def set_model_address(self, model_address: str | None) -> None:
        self._model_address = model_address

    def set_is_gemini(self, is_gemini: bool) -> None:
        self._is_gemini = is_gemini

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        token = _current_stream_seen_ids.set(set())
        try:
            yield from super()._stream(*args, **kwargs)
        finally:
            _current_stream_seen_ids.reset(token)

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        token = _current_stream_seen_ids.set(set())
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        finally:
            _current_stream_seen_ids.reset(token)

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type[Any],
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(  # pyright: ignore[reportUnknownMemberType]
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None or not isinstance(generation_chunk.message, AIMessageChunk):
            return generation_chunk

        chunk_nested: dict[str, Any] = (
            cast(dict[str, Any], chunk["chunk"]) if isinstance(chunk.get("chunk"), dict) else {}
        )
        choices_raw: object = chunk.get("choices") or chunk_nested.get("choices")
        if not isinstance(choices_raw, list) or not choices_raw:
            return generation_chunk

        first_choice = cast(dict[str, Any], choices_raw[0])
        delta_raw = first_choice.get("delta")
        if not isinstance(delta_raw, dict):
            return generation_chunk
        delta = cast(dict[str, Any], delta_raw)

        raw_tool_calls_obj: object = delta.get("tool_calls")
        if not isinstance(raw_tool_calls_obj, list) or not raw_tool_calls_obj:
            return generation_chunk

        raw_tool_calls: list[object] = cast(list[object], raw_tool_calls_obj)

        seen_ids = _current_stream_seen_ids.get()
        if seen_ids is None:
            if base_generation_info:
                self._seen_call_ids.clear()
            seen_ids = self._seen_call_ids

        extra_by_key: dict[str, Any] = {}
        model_by_key: dict[str, str] = {}
        for rtc_obj in raw_tool_calls:
            if not isinstance(rtc_obj, dict):
                continue
            rtc = cast(dict[str, Any], rtc_obj)
            call_id = rtc.get("id")
            # The model address and each call's extra_content are set only on the
            # first delta that carries that call id; later deltas for the same id
            # never carry them again so LangChain chunk addition has nothing to concatenate.
            if not call_id:
                continue
            call_id_str = str(call_id)
            if call_id_str in seen_ids:
                continue
            seen_ids.add(call_id_str)

            idx_str = str(rtc.get("index", 0))
            extra = cast(dict[str, Any] | None, rtc.get("extra_content"))
            if extra is not None:
                extra_by_key[idx_str] = extra
                extra_by_key[call_id_str] = extra
            if self._model_address:
                model_by_key[idx_str] = self._model_address
                model_by_key[call_id_str] = self._model_address

        if extra_by_key:
            generation_chunk.message.additional_kwargs.setdefault(
                "tool_calls_extra_content", {}
            ).update(extra_by_key)
        if model_by_key:
            generation_chunk.message.additional_kwargs.setdefault("tool_calls_model", {}).update(
                model_by_key
            )

        return generation_chunk

    def _create_chat_result(
        self,
        response: dict[str, Any] | openai.BaseModel,
        generation_info: dict[str, Any] | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)  # pyright: ignore[reportUnknownMemberType]
        response_dict: dict[str, Any] = (
            response if isinstance(response, dict) else response.model_dump(warnings=False)
        )
        choices_obj = response_dict.get("choices", [])
        choices: list[object] = (
            cast(list[object], choices_obj) if isinstance(choices_obj, list) else []
        )
        for res_obj, gen in zip(choices, result.generations, strict=False):
            if not isinstance(res_obj, dict):
                continue
            res = cast(dict[str, Any], res_obj)
            res_msg = res.get("message")
            raw_tcs_obj = (
                cast(dict[str, Any], res_msg).get("tool_calls")
                if isinstance(res_msg, dict)
                else None
            )
            raw_tcs: list[object] = (
                cast(list[object], raw_tcs_obj) if isinstance(raw_tcs_obj, list) else []
            )
            extra_by_key: dict[str, Any] = {}
            model_by_key: dict[str, str] = {}
            for idx, tc_obj in enumerate(raw_tcs):
                if not isinstance(tc_obj, dict):
                    continue
                tc = cast(dict[str, Any], tc_obj)
                extra = cast(dict[str, Any] | None, tc.get("extra_content"))
                cid = tc.get("id")
                idx_str = str(idx)
                if extra is not None:
                    extra_by_key[idx_str] = extra
                    if cid:
                        extra_by_key[str(cid)] = extra
                    if self._model_address:
                        model_by_key[idx_str] = self._model_address
                        if cid:
                            model_by_key[str(cid)] = self._model_address
            if extra_by_key and isinstance(gen.message, AIMessage):
                gen.message.additional_kwargs.setdefault("tool_calls_extra_content", {}).update(
                    extra_by_key
                )
                if model_by_key:
                    gen.message.additional_kwargs.setdefault("tool_calls_model", {}).update(
                        model_by_key
                    )
        return result

    def _model_matches(self, call_model: str | None) -> bool:
        if call_model is None:
            return True
        target = self._model_address or self.model_name
        return call_model == target or call_model == self.model_name

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._seen_call_ids.clear()
        payload = cast(
            dict[str, Any],
            super()._get_request_payload(input_, stop=stop, **kwargs),  # pyright: ignore[reportUnknownMemberType]
        )
        messages = self._convert_input(input_).to_messages()  # pyright: ignore[reportUnknownMemberType]
        payload_messages = cast(list[dict[str, Any]], payload.get("messages") or [])

        for msg, p_msg in zip(messages, payload_messages, strict=False):
            if not (isinstance(msg, AIMessage) and p_msg.get("role") == "assistant"):
                continue
            raw_extra_content: dict[str, Any] = msg.additional_kwargs.get(
                "tool_calls_extra_content", {}
            )
            model_map: dict[str, str] = msg.additional_kwargs.get("tool_calls_model", {})

            for idx, tc in enumerate(cast(list[dict[str, Any]], p_msg.get("tool_calls", []))):
                call_id = tc.get("id")
                idx_str = str(idx)
                extra = None
                if call_id and call_id in raw_extra_content:
                    extra = raw_extra_content[call_id]
                elif idx_str in raw_extra_content:
                    extra = raw_extra_content[idx_str]

                call_model = None
                if call_id and call_id in model_map:
                    call_model = model_map[call_id]
                elif idx_str in model_map:
                    call_model = model_map[idx_str]

                if extra is not None and self._model_matches(call_model):
                    tc["extra_content"] = extra
                elif self._is_gemini:
                    tc["extra_content"] = {
                        "google": {"thought_signature": SKIP_THOUGHT_SIGNATURE_VALIDATOR}
                    }

        return payload


def is_gemini_model(
    base_url: str,
    wire_model_id: str,
    model_address: str | None = None,
    explicit_is_gemini: bool | None = None,
) -> bool:
    if explicit_is_gemini is not None:
        return explicit_is_gemini
    target = (model_address or wire_model_id).lower()
    return (
        "gemini" in target
        or "generativelanguage.googleapis.com" in base_url
        or "/v1beta/openai" in base_url
    )


def build_chat_model(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 120.0,
    max_tokens: int | None = None,
    model_address: str | None = None,
    is_gemini: bool | None = None,
) -> ChatOpenAI:
    """The only way the app creates an LLM client. Retries are handled by the caller. `http` is
    the app's shared client (see ProvidersService); without it langchain-openai would use a
    process-wide cached client tied to the first event loop."""
    model = ChatOpenAIWithExtraContent(
        base_url=base_url,
        api_key=SecretStr(api_key or KEYLESS),
        model=wire_model_id,
        streaming=True,
        stream_usage=True,
        max_retries=0,
        timeout=timeout,
        http_async_client=http,
        max_completion_tokens=max_tokens,
    )
    model.set_model_address(model_address or wire_model_id)
    gemini_flag = (
        is_gemini
        if is_gemini is not None
        else is_gemini_model(base_url, wire_model_id, model_address)
    )
    model.set_is_gemini(gemini_flag)
    return model


def build_embeddings(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 120.0,
) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        base_url=base_url,
        api_key=SecretStr(api_key or KEYLESS),
        model=wire_model_id,
        max_retries=0,
        timeout=timeout,
        http_async_client=http,
        check_embedding_ctx_length=False,
        chunk_size=EMBED_BATCH,
    )


@dataclass(frozen=True)
class ResolvedModel:
    address: str
    provider_id: str
    provider_name: str
    base_url: str
    wire_model_id: str
    context_window: int
    keys: tuple[str, ...] = field(repr=False)
    request_token_limit: int | None = None

    def build(self, http: httpx2.AsyncClient, key_index: int = 0) -> ChatOpenAI:
        from mlops_agents.agents.runtime.budget import reply_reserve_tokens, request_budget_tokens

        key = self.keys[key_index] if self.keys else None
        max_tokens = (
            reply_reserve_tokens(request_budget_tokens(self))
            if self.request_token_limit is not None
            else None
        )
        is_gem = self.provider_name == "gemini" or "gemini" in self.address.lower()
        return build_chat_model(
            self.base_url,
            key,
            self.wire_model_id,
            http=http,
            max_tokens=max_tokens,
            model_address=self.address,
            is_gemini=is_gem,
        )
