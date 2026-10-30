"""A fake proposal kind under a real kind's name, for one block of a test."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from mlops_agents.proposals.contract import ProposalKind
from mlops_agents.proposals.kinds import ProposalKinds


@contextmanager
def swapped_kind(kinds: ProposalKinds, fake: ProposalKind) -> Generator[ProposalKind]:
    """Registers `fake` in place of the kind of the same name, and puts that one back on exit
    (or removes `fake` when there was none). Production keeps refusing a second registration."""
    # Known debt (Decision #535): the registry has no remove, by design, so this reaches into it.
    registered = kinds._kinds
    real = registered.pop(fake.name, None)
    kinds.register(fake)
    try:
        yield fake
    finally:
        registered.pop(fake.name, None)
        if real is not None:
            registered[fake.name] = real
