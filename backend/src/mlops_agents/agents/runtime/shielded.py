from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Callable, Coroutine
from typing import Any


def shielded[**P, R](
    write: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Finish a database write before a cancel reaches its caller.

    Stop and shutdown cancel a turn at any await. A cancel that landed inside a write left the
    writes after it failing with 'database is locked', so the turn ended failed instead of
    stopped. The write runs to its end first and then the cancel is raised; the write's own
    error, if it has one, is raised in its place. The caller (which holds the session lock)
    never lets go while a write is still in flight.
    """

    @functools.wraps(write)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        task = asyncio.ensure_future(write(*args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            task.result()
            raise

    return run
