"""The notifications link to an incident must open the frontend's incident page."""

from __future__ import annotations

import re
from pathlib import Path

from mlops_agents.notifications.constants import INCIDENT_PATH

ROUTE_FILE = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "pages" / "incidents" / "route.tsx"
)


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def test_incident_path_matches_the_frontend_route() -> None:
    match = re.search(r'path:\s*"([^"]+)"', ROUTE_FILE.read_text(encoding="utf-8"))
    assert match is not None, f"no route path found in {ROUTE_FILE}"
    route_path = re.sub(
        r":(\w+)", lambda m: "{" + _camel_to_snake(m.group(1)) + "}", match.group(1)
    )
    assert route_path == INCIDENT_PATH
