"""Knowledge limits and constants."""

from __future__ import annotations

# sqlite-vec limit on vector dimensions
MAX_DIMENSIONS: int = 8192

# Reserved limits for knowledge notes and hybrid search
TITLE_MAX_CHARS: int = 200
TEXT_MAX_CHARS: int = 8000
RRF_K: int = 60
SEARCH_CANDIDATES: int = 50
VECTOR_CANDIDATES: int = 50
RETRY_AFTER_SECONDS: float = 60.0
EMBED_INPUT_CHARS: int = 8000
EMBED_BATCH: int = 16
MAX_QUERY_CHARS: int = 2000
MAX_QUERY_TOKENS: int = 32
SEARCH_MAX_LIMIT: int = 20
