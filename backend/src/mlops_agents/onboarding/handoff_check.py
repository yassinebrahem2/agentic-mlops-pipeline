"""The handoff's references to the project's connections: only what needs project state.

The payload's own validation (references between its parts, time zones, cadences, commands) is
the payload model's and is not repeated here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from mlops_agents.proposals.payloads import DataFeedRef, FileResults, HandoffPayload


def table_references(payload: HandoffPayload) -> list[tuple[str, str, str]]:
    """(part, connection, table) for every reference that names a table."""
    found = [
        (f"data role {r.connection}.{r.table}", r.connection, r.table) for r in payload.data_roles
    ]
    for feed in payload.operations.feeds:
        if isinstance(feed.ref, DataFeedRef):
            found.append((f"feed {feed.name}", feed.ref.connection, feed.ref.table))
    return found


def _file_references(payload: HandoffPayload) -> list[tuple[str, str]]:
    """(part, connection) for every reference that names only a connection."""
    ops = payload.operations
    found: list[tuple[str, str]] = []
    for feed in ops.feeds:
        if not isinstance(feed.ref, DataFeedRef) and feed.ref.file is not None:
            found.append((f"feed {feed.name}", feed.ref.file.connection))
    for check in ops.checks:
        if isinstance(check.results, FileResults):
            found.append((f"check {check.check_id}", check.results.connection))
    if isinstance(ops.results_location, FileResults):
        found.append(("results location", ops.results_location.connection))
    return found


def check(
    payload: HandoffPayload,
    connections: Collection[str],
    tables: Mapping[str, Collection[str] | str],
) -> list[str]:
    """One line per problem, naming the part. `tables` maps a connection to its table names, or
    to the reason they could not be read."""
    problems: list[str] = []
    for part, connection, table in table_references(payload):
        if connection not in connections:
            problems.append(f"{part}: needs the connection {connection} first")
            continue
        known = tables.get(connection, ())
        if isinstance(known, str):
            problems.append(
                f"{part}: the tables of the connection {connection} are unknown ({known})"
            )
        elif table not in known:
            problems.append(f"{part}: the connection {connection} has no table {table}")
    for part, connection in _file_references(payload):
        if connection not in connections:
            problems.append(f"{part}: needs the connection {connection} first")
    return problems
