from __future__ import annotations

from dataclasses import dataclass, field

from mlops_agents.agents.runtime.contract import Part


@dataclass
class TurnMessage:
    """A turn's agent message as its run builds it: its id once stored, and its live parts.

    One object shared by the executor, the turn graph and its tool step. A message taken into
    the running turn splits it (the graph finishes this message and continues the turn in a new
    one), so every writer reads `id` at the moment it writes and `parts` is cleared in place.
    """

    id: str | None = None
    parts: list[Part] = field(default_factory=lambda: [])

    def current_id(self) -> str:
        if self.id is None:
            raise RuntimeError("The turn has no agent message yet.")
        return self.id
