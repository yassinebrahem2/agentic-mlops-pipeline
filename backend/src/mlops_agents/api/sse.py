"""Server-Sent Events helpers and response classes shared across routers."""

from __future__ import annotations

from typing import Final

from sse_starlette import EventSourceResponse

# A comment line this often keeps proxies from closing an idle stream.
PING_SECONDS: Final[float] = 15.0
SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def parse_after_seq(last_event_id: str | None, after: str | None) -> int | None:
    raw = last_event_id if last_event_id is not None else after
    # int() would also accept "+5", "1_0" and non-ASCII digits, which are not seqs.
    if raw is None or not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw)


class SseResponse(EventSourceResponse):
    media_type = "text/event-stream"
