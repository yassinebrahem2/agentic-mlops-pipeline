"""Full-text search (FTS5) query building (I17)."""

from __future__ import annotations

import re

from mlops_agents.knowledge.limits import MAX_QUERY_CHARS, MAX_QUERY_TOKENS


def build_fts_query(text: str) -> str | None:
    """Turn free text into an FTS5 MATCH expression.

    Extracts Unicode word tokens, lowercases and deduplicates them in order,
    cuts to MAX_QUERY_TOKENS, and joins each quoted token with ' OR '.
    Returns None if text contains no word tokens.
    """
    if not text:
        return None

    clipped = text[:MAX_QUERY_CHARS]
    raw_tokens = re.findall(r"\w+", clipped)
    if not raw_tokens:
        return None

    seen: set[str] = set()
    deduped: list[str] = []
    for raw in raw_tokens:
        tok = raw.lower()
        if tok not in seen:
            seen.add(tok)
            deduped.append(tok)
            if len(deduped) == MAX_QUERY_TOKENS:
                break

    if not deduped:
        return None

    return " OR ".join(f'"{tok}"' for tok in deduped)
