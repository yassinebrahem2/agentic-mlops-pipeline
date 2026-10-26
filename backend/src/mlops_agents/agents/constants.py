from __future__ import annotations

__all__ = ["MAX_DIFF_CHARS", "cap_diff"]

MAX_DIFF_CHARS = 30_000


def cap_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    """Caps diff output to max_chars, keeping head and tail and noting omitted character count."""
    if len(diff) <= max_chars:
        return diff
    half = max_chars // 2
    cut = len(diff) - max_chars
    return diff[:half] + f"\n\n[... diff cut: {cut} characters omitted ...]\n\n" + diff[-half:]
