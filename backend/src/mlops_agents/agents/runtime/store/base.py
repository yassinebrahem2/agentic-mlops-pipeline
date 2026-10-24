from __future__ import annotations

from mlops_agents.storage.databases import Databases


class StoreBase:
    """What every table group of the session store shares: the project databases."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases
