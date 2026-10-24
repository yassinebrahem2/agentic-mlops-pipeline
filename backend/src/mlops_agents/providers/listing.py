from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ValidationError

from mlops_agents.credentials.scrub import error_text, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.providers.models import Provider, RemoteModel

_log = logging.getLogger(__name__)


def normalize_model_id(model_id: str) -> str:
    """Strip whitespace and optional leading 'models/' prefix from a model identifier."""
    return model_id.strip().removeprefix("models/")


def match_model_id(a: str, b: str) -> bool:
    """True when two model identifiers match, with or without a leading 'models/' prefix."""
    return normalize_model_id(a) == normalize_model_id(b)


class ListedModel(BaseModel):
    """One entry of a provider's /models answer. Providers name the context window differently."""

    id: str
    context_window: object = None
    context_length: object = None
    max_context_length: object = None
    max_model_len: object = None

    def context(self) -> int | None:
        for value in (
            self.context_window,
            self.context_length,
            self.max_context_length,
            self.max_model_len,
        ):
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return None


class _ModelList(BaseModel):
    data: list[object]


class _NativeModel(BaseModel):
    name: str
    inputTokenLimit: object = None

    def context(self) -> int | None:
        if (
            isinstance(self.inputTokenLimit, int)
            and not isinstance(self.inputTokenLimit, bool)
            and self.inputTokenLimit > 0
        ):
            return self.inputTokenLimit
        return None


class _NativeModelList(BaseModel):
    models: list[object] = []
    nextPageToken: str | None = None


def is_gemini_shaped(base_url: str) -> bool:
    """True if base URL path ends with /v1beta/openai."""
    parsed = urlsplit(base_url)
    return parsed.path.rstrip("/").endswith("/v1beta/openai")


def gemini_native_models_url(base_url: str) -> str:
    """Derive the native /v1beta/models URL from a Gemini OpenAI base URL."""
    parsed = urlsplit(base_url)
    clean_path = parsed.path.rstrip("/")
    prefix = clean_path[: -len("/v1beta/openai")]
    return f"{parsed.scheme}://{parsed.netloc}{prefix}/v1beta/models"


async def fetch_gemini_native_windows(
    base_url: str,
    key: str | None,
    http: httpx2.AsyncClient,
    *,
    provider_name: str = "gemini",
) -> list[tuple[str, int]]:
    """Fetch native model windows from Gemini's /v1beta/models, following nextPageToken.

    Returns a list of (model_name, input_token_limit) pairs.
    If the native call fails for any reason (e.g. HTTP 500, network error),
    logs a warning without secrets and returns an empty list without failing the caller.
    """
    native_url = gemini_native_models_url(base_url)
    headers: dict[str, str] = {}
    if key:
        headers["x-goog-api-key"] = key

    results: list[tuple[str, int]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()

    while True:
        params: dict[str, str] = {}
        if page_token:
            params["pageToken"] = page_token

        try:
            response = await http.get(native_url, headers=headers, params=params, timeout=20)
        except httpx2.HTTPError as exc:
            reason = scrub_values(error_text(exc), [key] if key else [])
            _log.warning("Could not reach native model listing for %s: %s", provider_name, reason)
            return []

        if response.status_code >= 400:
            _log.warning(
                "Native model listing for %s answered with HTTP %d",
                provider_name,
                response.status_code,
            )
            return []

        try:
            data = response.json()
        except ValueError:
            _log.warning("Native model listing for %s did not answer with JSON", provider_name)
            return []

        try:
            listing = _NativeModelList.model_validate(data)
        except ValidationError:
            _log.warning("Native model listing for %s had unexpected schema", provider_name)
            return []

        for item in listing.models:
            try:
                native_item = _NativeModel.model_validate(item)
            except ValidationError:
                continue
            ctx = native_item.context()
            if ctx is not None:
                results.append((native_item.name, ctx))

        page_token = listing.nextPageToken
        if not page_token or page_token in seen_tokens:
            break
        seen_tokens.add(page_token)

    return results


async def list_remote_models(
    provider: Provider,
    key: str | None,
    *,
    get: Callable[[Provider, str, str | None], Awaitable[object]],
    http: httpx2.AsyncClient,
) -> list[RemoteModel]:
    """List remote models for a provider and populate context windows."""
    payload = await get(provider, "/models", key)
    try:
        listing = _ModelList.model_validate(payload)
    except ValidationError:
        raise DomainError(
            "provider_error", f"{provider.name} did not answer with a model list.", status=502
        ) from None

    found: list[RemoteModel] = []
    seen_ids: set[str] = set()
    for item in listing.data:
        try:
            listed = ListedModel.model_validate(item)
        except ValidationError:
            continue
        norm_id = normalize_model_id(listed.id)
        if norm_id in seen_ids:
            existing = next(m for m in found if m.id == norm_id)
            if existing.context_window is None and listed.context() is not None:
                existing.context_window = listed.context()
            continue
        seen_ids.add(norm_id)
        found.append(RemoteModel(id=norm_id, context_window=listed.context()))

    if is_gemini_shaped(provider.base_url):
        native_windows = await fetch_gemini_native_windows(
            provider.base_url,
            key,
            http,
            provider_name=provider.name,
        )
        if native_windows:
            for model in found:
                if model.context_window is None:
                    matched_win = next(
                        (win for name, win in native_windows if match_model_id(name, model.id)),
                        None,
                    )
                    if matched_win is not None:
                        model.context_window = matched_win

    return sorted(found, key=lambda m: m.id)
