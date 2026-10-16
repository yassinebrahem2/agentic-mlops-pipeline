"""Keeping secrets out of URLs and text: base URL validation, URL and value scrubbing."""

from __future__ import annotations

import base64
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import SplitResult, quote, quote_plus, unquote_plus, urlsplit

MASK = "***"

# Parameter names that carry a credential, compared after _normalized_name. Credentials belong
# in the key store, never in a base URL; this list is defense in depth and cannot be complete.
_CREDENTIAL_PARAMS = frozenset(
    {
        "api_key", "apikey", "api_token", "x_api_key", "x_goog_api_key", "key",
        "token", "access_token", "refresh_token", "id_token", "auth_token",
        "session_token", "hf_token", "jwt", "secret", "client_secret", "private_key",
        "password", "passwd", "pwd", "passphrase", "credential", "credentials",
        "sig", "signature", "sas", "auth", "authorization", "bearer", "code",
        "subscription_key", "ocp_apim_subscription_key", "x_amz_signature",
        "x_amz_credential", "x_amz_security_token",
    }
)  # fmt: skip
_ZERO_WIDTH: dict[int, None] = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
_CREDENTIALS_IN_URL = (
    "base_url must not carry credentials (user:secret@host); add the key as a provider key"
)
_UNPARSEABLE = "base_url is not a valid URL (it needs a host and at most a numeric port)"
# URL userinfo anywhere in free text, greedy to the last "@" of a whitespace-free run.
_USERINFO_IN_TEXT = re.compile(r"(?<=://)\S*@")
# A query or fragment parameter in free text: (separator + name + "=", value).
_PARAM_IN_TEXT = re.compile(
    r"([?&;#][^=&;#?\s'\"]+=)"
    r"(\\?(['\"]).*?\\?\3[^&;#\s]*|[^&;#\s]*)"
)
_PARAM_SEPARATOR = re.compile(r"[&;]")


def _normalized_name(raw: str) -> str:
    name = unicodedata.normalize("NFKC", unquote_plus(raw)).translate(_ZERO_WIDTH)
    name = re.sub(r"[-+.\s]", "_", name.strip().lower())
    name = re.sub(r"(\[[^\[\]]*\])+$", "", name)
    return re.sub(r"_+", "_", name).strip("_")


def _name_problem(raw: str) -> str | None:
    name = _normalized_name(raw)
    if name in _CREDENTIAL_PARAMS:
        return (
            "base_url must not carry a credential in a query, fragment or path "
            f"parameter (parameter {name!r}); add the key as a provider key"
        )
    decoded = unquote_plus(raw)
    if not decoded.isascii() or any(
        c.isspace() or unicodedata.category(c).startswith("C") for c in decoded
    ):
        return (
            "base_url has a parameter whose decoded name is not plain ASCII; it cannot be "
            "checked for a credential, so it is refused"
        )
    return None


def _param_names(parts: SplitResult) -> list[str]:
    names = [
        p.partition("=")[0]
        for section in (parts.query, parts.fragment)
        for p in _PARAM_SEPARATOR.split(section)
        if p
    ]
    for segment in parts.path.split("/"):
        names.extend(p.partition("=")[0] for p in segment.split(";")[1:] if p)
    return names


def base_url_problem(url: str) -> str | None:
    """Why `url` cannot be a provider base URL, or None. Never repeats any part of the URL."""
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in url):
        return "base_url must not contain whitespace or control characters"
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
        host = parts.hostname
    except ValueError:
        return _UNPARSEABLE
    if parts.scheme not in ("http", "https"):
        return "base_url must start with http:// or https://"
    if "@" in parts.netloc:
        return _CREDENTIALS_IN_URL
    if not host:
        return "base_url has no host"
    for raw in _param_names(parts):
        problem = _name_problem(raw)
        if problem is not None:
            return problem
    return None


def scrub_url_secrets(text: str) -> str:
    """Every URL userinfo and every query or fragment parameter value in `text` masked."""
    text = _USERINFO_IN_TEXT.sub("***@", text)
    return _PARAM_IN_TEXT.sub(lambda m: m.group(1) + MASK, text)


def error_text(exc: BaseException) -> str:
    """`str(exc)` made safe to show. URL-parse errors are reduced to their kind, because their
    text can quote part of a key."""
    import httpx

    if isinstance(exc, httpx.InvalidURL):
        return "the URL could not be parsed (its text is withheld: it can quote part of a key)"
    return scrub_url_secrets(str(exc))


def _lower_hex(text: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), text)


def _forms(secret: str) -> set[str]:
    """`secret` verbatim, percent-encoded three ways (upper and lower hex), and base64 of
    `s`, `":" + s` and `s + ":"` (Basic auth with an empty user or password)."""
    percent = {quote(secret, safe=""), quote(secret), quote_plus(secret)}
    b64 = {base64.b64encode(raw.encode()).decode() for raw in (secret, ":" + secret, secret + ":")}
    return {secret, *percent, *map(_lower_hex, percent), *b64}


def scrub_values(text: str, secrets: Iterable[str]) -> str:
    """`text` with every occurrence of every non-empty secret, and its encoded forms, masked.
    Overlapping spans are merged first so no fragment of a secret survives."""
    spans: list[tuple[int, int]] = []
    for secret in {form for s in secrets if s for form in _forms(s)}:
        start = text.find(secret)
        while start != -1:
            spans.append((start, start + len(secret)))
            start = text.find(secret, start + 1)
    if not spans:
        return text
    spans.sort()
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    out: list[str] = []
    pos = 0
    for lo, hi in merged:
        out.append(text[pos:lo])
        out.append(MASK)
        pos = hi
    out.append(text[pos:])
    return "".join(out)


def mask_key(value: str) -> str:
    """A hint for the UI: the last 4 characters, only when at least as many stay hidden."""
    return "••••" + value[-4:] if len(value) >= 8 else "••••"
