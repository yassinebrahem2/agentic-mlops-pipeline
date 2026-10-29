"""Reciprocal Rank Fusion (RRF) for search result rankings (I17)."""

from __future__ import annotations

from collections.abc import Sequence

from mlops_agents.knowledge.limits import RRF_K


def fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[str]:
    """Fuse multiple ranked lists of note IDs using Reciprocal Rank Fusion.

    Score for each note ID is sum(1 / (k + rank)) across rankings (1-based rank).
    Ties are broken by the ID's position in the first ranking that holds it, then by ID.
    Pure and deterministic; no I/O.
    """
    scores: dict[str, float] = {}
    first_pos: dict[str, int] = {}

    for ranking in rankings:
        seen_in_ranking: set[str] = set()
        for pos, nid in enumerate(ranking):
            if nid in seen_in_ranking:
                continue
            seen_in_ranking.add(nid)
            rank = pos + 1
            scores[nid] = scores.get(nid, 0.0) + (1.0 / (k + rank))
            if nid not in first_pos:
                first_pos[nid] = pos

    return sorted(
        scores.keys(),
        key=lambda nid: (-scores[nid], first_pos[nid], nid),
    )
