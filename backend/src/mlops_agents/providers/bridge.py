from __future__ import annotations

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


# Third-party boundary: LangChain OpenAI (version 1.7.2).
# Overrides ChatOpenAI._convert_chunk_to_generation_chunk, _create_chat_result, and _get_request_payload.
# LangChain drops extra_content from tool calls during streaming parse (_convert_delta_to_message_chunk)
# and request serialization (_lc_tool_call_to_openai_tool_call). Gemini 3 requires tool call extra_content
# (such as google.thought_signature) preserved and sent back on subsequent requests to the same model.
class ChatOpenAIWithExtraContent(ChatOpenAI):
    _model_address: str | None = PrivateAttr(default=None)

    def set_model_address(self, model_address: str | None) -> None:
        self._model_address = model_address

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

        extra_by_key: dict[str, Any] = {}
        model_by_key: dict[str, str] = {}
        for rtc_obj in raw_tool_calls:
            if not isinstance(rtc_obj, dict):
                continue
            rtc = cast(dict[str, Any], rtc_obj)
            idx_str = str(rtc.get("index", 0))
            extra = cast(dict[str, Any] | None, rtc.get("extra_content"))
            if extra is not None:
                extra_by_key[idx_str] = extra
            if self._model_address and rtc.get("id"):
                model_by_key[idx_str] = self._model_address

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
        payload = cast(
            dict[str, Any],
            super()._get_request_payload(input_, stop=stop, **kwargs),  # pyright: ignore[reportUnknownMemberType]
        )
        messages = self._convert_input(input_).to_messages()  # pyright: ignore[reportUnknownMemberType]
        payload_messages = cast(list[dict[str, Any]], payload.get("messages") or [])

        for msg, p_msg in zip(messages, payload_messages, strict=False):
            if not (isinstance(msg, AIMessage) and p_msg.get("role") == "assistant"):
                continue
            extra_content_map: dict[str, Any] | None = msg.additional_kwargs.get(
                "tool_calls_extra_content"
            )
            if not extra_content_map:
                continue
            model_map: dict[str, str] = msg.additional_kwargs.get("tool_calls_model", {})
            for idx, tc in enumerate(cast(list[dict[str, Any]], p_msg.get("tool_calls", []))):
                call_id = tc.get("id")
                idx_str = str(idx)
                extra = None
                if call_id and call_id in extra_content_map:
                    extra = extra_content_map[call_id]
                elif idx_str in extra_content_map:
                    extra = extra_content_map[idx_str]

                if extra is None:
                    continue

                call_model = None
                if call_id and call_id in model_map:
                    call_model = model_map[call_id]
                elif idx_str in model_map:
                    call_model = model_map[idx_str]

                if self._model_matches(call_model):
                    tc["extra_content"] = extra

        return payload


def build_chat_model(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 120.0,
    model_address: str | None = None,
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
    )
    model.set_model_address(model_address or wire_model_id)
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

    def build(self, http: httpx2.AsyncClient, key_index: int = 0) -> ChatOpenAI:
        key = self.keys[key_index] if self.keys else None
        return build_chat_model(
            self.base_url, key, self.wire_model_id, http=http, model_address=self.address
        )
