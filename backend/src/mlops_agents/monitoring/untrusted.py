"""Wrap untrusted text in data tags, neutralising inner data tags."""

from __future__ import annotations

import re

# Untrusted data: a data tag inside it, opening or closing, in any letter case or
# spacing around its "/", must not end its block early or forge another, so its "<" becomes "<\".
# Only the tag name "data" counts: "<database>" or "a < data" are ordinary text.
_DATA_TAG = re.compile(r"<(?=(?:\s*/\s*)?data(?![\w-]))", re.IGNORECASE)


def untrusted(text: str, source: str) -> str:
    """Wraps text in a <data source="..."> block with inner <data tags neutralised."""
    return f'<data source="{source}">\n{_DATA_TAG.sub(r"<\\", text)}\n</data>'
