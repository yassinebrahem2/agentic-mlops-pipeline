"""Runs: each its own public copy of the demo repository and its own folders.

`new-run <name>` copies the template (the original demo repository) into the user's account,
clones the copy, creates empty data, MLflow and world folders beside it and writes every
setting to `run.env`, which `--run <name>` loads. So every run, and every rehearsal, starts
from the same company with no data and no model, and nothing is shared between runs.
"""

import re
import socket
import time
from collections.abc import Callable
from pathlib import Path

from demo_world import paths
from demo_world.company import SCHEDULE_FILE
from demo_world.github import GhRunner, call

RUN_FILE = "run.env"
REPOSITORY_PREFIX = "hotel-cancellation-demo-"
# Lowercase, digits and dashes: a valid repository name and folder name everywhere.
_RUN_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_FOLDERS = {
    paths.DATA_VARIABLE: "data",
    paths.MLFLOW_VARIABLE: "mlflow",
    paths.WORLD_VARIABLE: "world",
}
# GitHub fills a copy from its template after answering; a few seconds, usually.
READY_ATTEMPTS = 30
READY_WAIT_SECONDS = 2.0


class RunError(Exception):
    """A run cannot be created or loaded."""


def run_folder(name: str) -> Path:
    return paths.runs_dir() / name


def free_port() -> int:
    """A TCP port nothing listens on now, chosen by the system."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ports_of_other_runs() -> set[int]:
    # Two runs on one machine must not both start their MLflow server on one port.
    taken: set[int] = set()
    if not paths.runs_dir().is_dir():
        return taken
    for env_file in paths.runs_dir().glob(f"*/{RUN_FILE}"):
        port = _read(env_file).get(paths.MLFLOW_PORT_VARIABLE)
        if port is not None and port.isdigit():
            taken.add(int(port))
    return taken


def _pick_port(pick: Callable[[], int]) -> int:
    taken = _ports_of_other_runs()
    while (port := pick()) in taken:
        pass
    return port


def _wait_until_filled(gh: GhRunner, repository: str, sleep: Callable[[float], None]) -> None:
    for attempt in range(READY_ATTEMPTS):
        if gh(["api", f"repos/{repository}/commits?per_page=1"]).ok:
            return
        if attempt < READY_ATTEMPTS - 1:
            sleep(READY_WAIT_SECONDS)
    raise RunError(
        f"the copy {repository} still has no commit; delete it (`gh repo delete {repository}`) "
        "and try again"
    )


def _write(path: Path, settings: dict[str, str]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    text = "".join(f"{name}={settings[name]}\n" for name in paths.RUN_SETTINGS)
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def new_run(
    name: str,
    gh: GhRunner,
    *,
    free_port: Callable[[], int] = free_port,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Create the run `name`: its copy on GitHub, its clone, its folders and its run.env."""
    if not _RUN_NAME.fullmatch(name):
        raise RunError(
            f"run name {name!r}: use 1 to 40 lowercase letters, digits and dashes, "
            "starting with a letter or a digit"
        )
    folder = run_folder(name)
    if folder.exists():
        raise RunError(f"the run {name} already exists in {folder}; pick another name")
    owner = call(gh, ["api", "user", "--jq", ".login"]).stdout.strip()
    repository = f"{owner}/{REPOSITORY_PREFIX}{name}"
    generate = ["api", "-X", "POST", f"repos/{paths.ORIGINAL_REPOSITORY}/generate"]
    # Public, so the copy's Actions runs cost no minutes; `-F` sends a JSON false.
    generate += ["-f", f"owner={owner}", "-f", f"name={REPOSITORY_PREFIX}{name}"]
    call(gh, [*generate, "-F", "private=false"])
    _wait_until_filled(gh, repository, sleep)
    checkout = folder / "company"
    call(gh, ["repo", "clone", repository, str(checkout)])
    if not (checkout / SCHEDULE_FILE).exists():
        raise RunError(
            f"the clone {checkout} has no {SCHEDULE_FILE.as_posix()}; delete the copy "
            f"(`gh repo delete {repository}`) and {folder}, then try again"
        )
    settings = {
        paths.REPO_VARIABLE: repository,
        paths.COMPANY_VARIABLE: str(checkout),
        paths.MLFLOW_PORT_VARIABLE: str(_pick_port(free_port)),
    }
    for variable, folder_name in _FOLDERS.items():
        (folder / folder_name).mkdir()
        settings[variable] = str(folder / folder_name)
    _write(folder / RUN_FILE, settings)
    return folder


def _read(path: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        name, equals, value = line.partition("=")
        if not equals or name not in paths.RUN_SETTINGS:
            raise RunError(f"{path}, line {number}: {name!r} is not a run setting")
        settings[name] = value
    return settings


def load_run(name: str) -> dict[str, str]:
    """The settings of the run `name`, each of them present."""
    path = run_folder(name) / RUN_FILE
    if not path.exists():
        raise RunError(f"no run {name} ({path} is missing); create it: demo-world new-run {name}")
    settings = _read(path)
    missing = [variable for variable in paths.RUN_SETTINGS if variable not in settings]
    if missing:
        raise RunError(f"{path} lacks {', '.join(missing)}")
    return settings
