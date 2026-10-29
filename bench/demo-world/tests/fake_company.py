"""A stand-in for the company's environment, run as `python fake_company.py <program> <args>`.

It answers as `hotel-demo` and `mlflow` do, from the checkout it runs in (its working
directory): each call is appended to `calls.jsonl` there, with the environment the world
gave it and whether the day's files were in the data folder, and `failures.json` there lists
the starts of the commands that must fail. `deploy` marks a production model in the MLflow
folder, which the `mlflow` probe then finds. `mlflow server` starts a child process, as `uv
run` does, writes both process IDs to `server_pids.txt` and answers `/health` on its port
until it is stopped (or answers nothing when `server_silent` exists).
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CALLS = "calls.jsonl"
FAILURES = "failures.json"
SERVER_PIDS = "server_pids.txt"
SILENT = "server_silent"
ENVIRONMENT = ("HOTEL_DEMO_DATA", "HOTEL_DEMO_MLFLOW", "MLFLOW_TRACKING_URI", "VIRTUAL_ENV")
NOT_FOUND = (
    "mlflow.exceptions.MlflowException: Registered Model with name=hotel-cancellation not found"
)


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _hotel_demo(args: list[str], mlflow: Path) -> int:
    command = args[0]
    if command == "train":
        versions = len(list(mlflow.glob("version-*"))) + 1
        mlflow.mkdir(parents=True, exist_ok=True)
        (mlflow / f"version-{versions}").touch()
        print("training on the history")
        print(versions)
    elif command == "deploy":
        (mlflow / "mlflow.db").touch()
        (mlflow / "production").write_text(_option(args, "--version"), encoding="utf-8")
    return 0


def _mlflow(mlflow: Path) -> int:
    if (mlflow / "production").exists():
        return 0
    print(NOT_FOUND, file=sys.stderr)
    return 1


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"OK"
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _server(args: list[str], checkout: Path) -> int:
    sleeper = [sys.executable, "-c", "import time; time.sleep(600)"]
    child = subprocess.Popen(sleeper)  # noqa: S603 (the test's own interpreter)
    (checkout / SERVER_PIDS).write_text(f"{os.getpid()}\n{child.pid}\n", encoding="utf-8")
    if (checkout / SILENT).exists():
        return child.wait()
    HTTPServer(("127.0.0.1", int(_option(args, "--port"))), _Health).serve_forever()
    return 0


def main() -> int:
    program, *args = sys.argv[1:]
    checkout = Path.cwd()
    data = Path(os.environ["HOTEL_DEMO_DATA"])
    mlflow = Path(os.environ["HOTEL_DEMO_MLFLOW"])
    call: dict[str, object] = {"argv": [program, *args]}
    call |= {name: os.environ.get(name) for name in ENVIRONMENT}
    for option in ("--date", "--cutoff"):
        if option in args:
            day = _option(args, option)
            call["bookings_present"] = (data / "bookings" / f"{day}.parquet").exists()
            call["labels_present"] = (data / "labels" / f"{day}.parquet").exists()
    with (checkout / CALLS).open("a", encoding="utf-8") as file:
        file.write(json.dumps(call) + "\n")

    failures = checkout / FAILURES
    text = " ".join([program, *args])
    if failures.exists() and any(text.startswith(f) for f in json.loads(failures.read_text())):
        print(f"{program} failed on purpose", file=sys.stderr)
        return 3
    if program == "mlflow" and args[:1] == ["server"]:
        return _server(args, checkout)
    if program == "mlflow":
        return _mlflow(mlflow)
    return _hotel_demo(args, mlflow)


if __name__ == "__main__":
    sys.exit(main())
