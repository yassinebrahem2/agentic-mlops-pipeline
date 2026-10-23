from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any


def shielded[**P, R](
    write: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Run a database write to its end even when its caller is cancelled.

    A cancel that lands inside a write's transaction leaves the connection holding SQLite's
    write lock until it is garbage collected, and every later write then fails with
    'database is locked'. Stop and shutdown cancel a turn at any await, so the writes finish.
    """

    @functools.wraps(write)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        return await asyncio.shield(asyncio.ensure_future(write(*args, **kwargs)))

    return run
