"""The provider keys "detect keys" recognises in the environment."""

from __future__ import annotations

import re
from collections.abc import Mapping

from mlops_agents.credentials.scrub import mask_key
from mlops_agents.providers.models import DetectedKey

# Environment variables recognised by "detect keys": name -> (provider name, base URL).
KNOWN_ENV_KEYS: dict[str, tuple[str, str]] = {
    "GROQ_API_KEY": ("groq", "https://api.groq.com/openai/v1"),
    "GEMINI_API_KEY": ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "OPENROUTER_API_KEY": ("openrouter", "https://openrouter.ai/api/v1"),
}
_ENV_VAR = re.compile(r"^(?P<base>[A-Z0-9_]+?)(?:_\d+)?$")


def known_env_key(env_var: str) -> tuple[str, str] | None:
    """The (provider name, base URL) a variable like GROQ_API_KEY_2 belongs to, or None."""
    match = _ENV_VAR.match(env_var)
    return KNOWN_ENV_KEYS.get(match.group("base")) if match else None


def detect_env_keys(source: Mapping[str, str], existing: set[str]) -> list[DetectedKey]:
    """The recognised keys in `source`; `existing` holds the names of the providers already
    added."""
    found: list[DetectedKey] = []
    for base, (provider_name, base_url) in KNOWN_ENV_KEYS.items():
        pattern = re.compile(rf"^{re.escape(base)}(?:_(?P<num>\d+))?$")
        candidates: list[tuple[tuple[int, int], str, str]] = []
        for var, value in source.items():
            if not value:
                continue
            match = pattern.match(var)
            if match:
                num_str = match.group("num")
                sort_key = (-1, 0) if num_str is None else (int(num_str), 1)
                candidates.append((sort_key, var, value))
        candidates.sort(key=lambda c: c[0])
        for _, var, value in candidates:
            found.append(
                DetectedKey(
                    env_var=var,
                    provider_name=provider_name,
                    base_url=base_url,
                    hint=mask_key(value),
                    provider_exists=provider_name in existing,
                )
            )
    return found
