"""The approval observers (I13), held by the app's one approval store."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal

from mlops_agents.agents.approvals.contract import ApprovalObserver, ApprovalView
from mlops_agents.credentials.scrub import error_text

logger = logging.getLogger(__name__)


class ApprovalObservers:
    def __init__(self) -> None:
        self._observers: list[ApprovalObserver] = []

    def add(self, observer: ApprovalObserver) -> None:
        self._observers.append(observer)

    async def tell(self, event: Literal["asked", "decided"], views: Sequence[ApprovalView]) -> None:
        """Await each observer, in registration order, for each view. A failing observer undoes
        nothing (the transition is stored already) and does not stop the next: it is logged,
        its error text scrubbed."""
        for view in views:
            for observer in self._observers:
                call = observer.asked if event == "asked" else observer.decided
                try:
                    await call(view)
                except Exception as exc:
                    logger.error(
                        "An approval observer failed when %s %s was %s: %s: %s",
                        view.kind,
                        view.id,
                        event,
                        type(exc).__name__,
                        error_text(exc),
                    )
