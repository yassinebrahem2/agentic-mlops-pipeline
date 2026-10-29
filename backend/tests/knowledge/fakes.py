"""Fakes for knowledge tests (tasks 2, 3, 5)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from mlops_agents.providers.embedder import Embedder, EmbedderStatus, EmbedderUnavailable


class ConceptEmbedder:
    """Deterministic bag-of-concepts embedder implementing Embedder protocol."""

    def __init__(
        self,
        address: str,
        dimensions: int,
        concepts: dict[str, set[str]],
    ) -> None:
        self.address = address
        self.dimensions = dimensions
        self.concepts = {k: {w.lower() for w in words} for k, words in concepts.items()}
        self._concept_groups = list(self.concepts.values())
        self.raises_unavailable = False
        self.unavailable_message = "Embedder is temporarily unavailable"
        self.call_count = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.call_count += 1
        if self.raises_unavailable:
            raise EmbedderUnavailable(self.unavailable_message)

        results: list[list[float]] = []
        for text in texts:
            tokens = re.findall(r"\w+", text.lower())
            vec = [0.0] * self.dimensions
            for idx, word_set in enumerate(self._concept_groups):
                if idx >= self.dimensions:
                    break
                vec[idx] = float(sum(1 for t in tokens if t in word_set))
            results.append(vec)
        return results


class FakeEmbedders:
    """In-memory implementation of Embedders protocol with swappable embedders."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        problem: str | None = None,
    ) -> None:
        self._default_embedder = embedder
        self._project_embedders: dict[str, Embedder | None] = {}
        self._problem = problem

    def set_embedder(self, project_id: str, embedder: Embedder | None) -> None:
        self._project_embedders[project_id] = embedder

    def set_problem(self, problem: str | None) -> None:
        self._problem = problem

    async def for_project(self, project_id: str) -> Embedder | None:
        if project_id in self._project_embedders:
            return self._project_embedders[project_id]
        return self._default_embedder

    async def status(self, project_id: str) -> EmbedderStatus:
        emb = await self.for_project(project_id)
        if emb is None:
            return EmbedderStatus(
                configured=False,
                address=None,
                dimensions=None,
                problem=self._problem,
            )
        return EmbedderStatus(
            configured=True,
            address=emb.address,
            dimensions=emb.dimensions,
            problem=self._problem,
        )


async def no_secrets() -> Sequence[str]:
    """Helper returning empty secrets for KnowledgeService."""
    return []
