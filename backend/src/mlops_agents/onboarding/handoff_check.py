"""The handoff's references to the project's connections: only what needs project state.

The payload's own validation (references between its parts, time zones, cadences, commands) is
the payload model's and is not repeated here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import PurePosixPath
from typing import Final

from mlops_agents.proposals.payloads import DataFeedRef, FileResults, HandoffPayload, JobFeedRef

VALID_JOB_FEED_EXTENSIONS: Final = {".jsonl", ".json", ".csv", ".log"}


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

    for feed in payload.operations.feeds:
        if feed.kind == "job" and isinstance(feed.ref, JobFeedRef) and feed.ref.file is not None:
            ext = PurePosixPath(feed.ref.file.path_pattern).suffix.lower()
            if ext and not any(c in ext for c in "*?[{") and ext not in VALID_JOB_FEED_EXTENSIONS:
                problems.append(
                    f"feed {feed.name}: a job feed reads result lines; "
                    "for a table that arrives each day use a data feed (connection, table)"
                )

    if isinstance(payload.operations.results_location, FileResults):
        loc = payload.operations.results_location
        loc_conn = loc.connection.lower()
        loc_pattern = loc.path_pattern.strip("/").lower()
        for check_spec in payload.operations.checks:
            if (
                isinstance(check_spec.results, FileResults)
                and check_spec.results.connection.lower() == loc_conn
            ):
                check_pattern = check_spec.results.path_pattern.strip("/").lower()
                check_parent = str(PurePosixPath(check_pattern).parent)
                if check_pattern == loc_pattern or (
                    check_parent != "." and check_parent == loc_pattern.rstrip("/*")
                ):
                    problems.append(
                        "the results location is where the agents' own checks write; "
                        "the team's results are read through their checks' sources"
                    )
                    break

    return problems
