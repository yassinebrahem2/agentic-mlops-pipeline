from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Callable, Coroutine
from typing import Any


def shielded[**P, R](
    write: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Finish a step that must not be cut short before a cancel reaches its caller.

    Stop and shutdown cancel a turn at any await. A cancel that landed inside a database write
    left the writes after it failing with 'database is locked', so the turn ended failed
    instead of stopped. The step runs to its end first and then the cancel is raised; the
    step's own error, if it has one, is raised in its place. The caller (which holds the
    session lock) never lets go while a write is still in flight.

    The same holds for the turn worker's end of turn: an interrupted turn's cleanup and the
    report to every turn observer run whole. The step runs in its own task, so it must not
    take a lock its caller holds.
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
