"""A fake MLflow REST server: in-memory experiments, runs, models, metrics and artifacts.

Used by backend tests and end-to-end tests.
Run it alone with: python -m tests.fakes.fake_mlflow --port N
"""

from __future__ import annotations

import argparse
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


@dataclass
class FakeMlflowState:
    page_size: int = 100
    required_token: str | None = None
    fail_next: dict[str, int] = field(default_factory=dict[str, int])
    aliases_supported: bool = True
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    experiments: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    runs: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    metric_history: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict[tuple[str, str], list[dict[str, Any]]]
    )
    registered_models: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    model_versions: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    aliases: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    artifacts: dict[tuple[str, str], bytes] = field(default_factory=dict[tuple[str, str], bytes])

    _experiment_counter: int = 0
    _run_counter: int = 0

    def add_experiment(self, name: str, experiment_id: str | None = None) -> str:
        if experiment_id is None:
            experiment_id = str(self._experiment_counter)
            self._experiment_counter += 1
        self.experiments[experiment_id] = {
            "experiment_id": experiment_id,
            "name": name,
            "artifact_location": f"fake://artifacts/{experiment_id}",
            "lifecycle_stage": "active",
        }
        return experiment_id

    def add_run(
        self,
        experiment_id: str,
        run_id: str | None = None,
        status: str = "RUNNING",
        start_time: int | None = None,
        end_time: int | None = None,
        params: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
        datasets: list[str] | None = None,
    ) -> str:
        if run_id is None:
            run_id = f"run_{self._run_counter}"
            self._run_counter += 1

        if start_time is None:
            start_time = int(time.time() * 1000)

        param_list = [{"key": k, "value": v} for k, v in (params or {}).items()]
        tag_list = [{"key": k, "value": v} for k, v in (tags or {}).items()]

        dataset_inputs: list[dict[str, Any]] = []
        if datasets:
            for ds in datasets:
                dataset_inputs.append({"dataset": {"name": ds}})

        info: dict[str, Any] = {
            "run_id": run_id,
            "run_uuid": run_id,
            "experiment_id": experiment_id,
            "status": status,
            "start_time": start_time,
        }
        if end_time is not None:
            info["end_time"] = end_time

        run_obj: dict[str, Any] = {
            "info": info,
            "data": {
                "metrics": [],
                "params": param_list,
                "tags": tag_list,
            },
            "inputs": {
                "dataset_inputs": dataset_inputs,
            },
        }
        self.runs[run_id] = run_obj
        return run_id

    def finish_run(
        self, run_id: str, status: str = "FINISHED", end_time: int | None = None
    ) -> None:
        if run_id not in self.runs:
            raise KeyError(f"Run {run_id} not found")
        if end_time is None:
            end_time = int(time.time() * 1000)
        self.runs[run_id]["info"]["status"] = status
        self.runs[run_id]["info"]["end_time"] = end_time

    def log_metric(
        self,
        run_id: str,
        key: str,
        value: float,
        timestamp: int | None = None,
        step: int = 0,
    ) -> None:
        if timestamp is None:
            timestamp = int(time.time() * 1000)
        point = {
            "key": key,
            "value": value,
            "timestamp": timestamp,
            "step": step,
        }
        self.metric_history.setdefault((run_id, key), []).append(point)

        # Update latest metric value in run data
        if run_id in self.runs:
            metrics_list: list[dict[str, Any]] = self.runs[run_id]["data"]["metrics"]
            # Replace existing metric for this key or append
            for idx, m in enumerate(metrics_list):
                if m["key"] == key:
                    metrics_list[idx] = point
                    break
            else:
                metrics_list.append(point)

    def register_version(
        self,
        model: str,
        version: str,
        run_id: str | None = None,
        creation_timestamp: int | None = None,
    ) -> None:
        if model not in self.registered_models:
            self.registered_models[model] = {"name": model}
        if creation_timestamp is None:
            creation_timestamp = int(time.time() * 1000)

        version_obj = {
            "name": model,
            "version": version,
            "run_id": run_id or "",
            "creation_timestamp": creation_timestamp,
        }
        self.model_versions.setdefault(model, []).append(version_obj)

    def set_alias(self, model: str, alias: str, version: str) -> None:
        if model not in self.registered_models:
            self.registered_models[model] = {"name": model}
        self.aliases.setdefault(model, {})[alias] = version

    def delete_alias(self, model: str, alias: str) -> None:
        if model in self.aliases:
            self.aliases[model].pop(alias, None)

    def put_artifact(self, run_id: str, path: str, content: bytes) -> None:
        self.artifacts[(run_id, path)] = content

    def reset(self) -> None:
        self.page_size = 100
        self.required_token = None
        self.fail_next.clear()
        self.aliases_supported = True
        self.requests.clear()
        self.experiments.clear()
        self.runs.clear()
        self.metric_history.clear()
        self.registered_models.clear()
        self.model_versions.clear()
        self.aliases.clear()
        self.artifacts.clear()
        self._experiment_counter = 0
        self._run_counter = 0


def create_fake_mlflow_app(state: FakeMlflowState) -> FastAPI:
    app = FastAPI(title="Fake MLflow")

    def _check_common(request: Request) -> Response | None:
        auth = request.headers.get("authorization", "")
        if state.required_token is not None and auth != f"Bearer {state.required_token}":
            return JSONResponse(
                {"error_code": "UNAUTHENTICATED", "message": "The tracker rejected the credential"},
                status_code=401,
            )

        # Check fail_next
        path = request.url.path
        for suffix in list(state.fail_next.keys()):
            if path.endswith(suffix):
                status_code = state.fail_next.pop(suffix)
                return JSONResponse(
                    {"error_code": "INTERNAL_ERROR", "message": f"Scripted failure {status_code}"},
                    status_code=status_code,
                )
        return None

    @app.middleware("http")
    async def log_and_intercept(request: Request, call_next: Any) -> Response:
        state.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "headers": dict(request.headers),
            }
        )
        err = _check_common(request)
        if err is not None:
            return err
        return await call_next(request)  # type: ignore[no-any-return]

    @app.get("/redirect-test")
    async def redirect_test() -> Response:
        return Response(status_code=302, headers={"location": "http://127.0.0.1:9999/other"})

    @app.post("/api/2.0/mlflow/experiments/search")
    async def search_experiments(request: Request) -> Response:
        try:
            body: dict[str, Any] = cast(dict[str, Any], await request.json())
        except Exception:
            body = {}
        max_results = min(int(body.get("max_results") or state.page_size), state.page_size)
        page_token = body.get("page_token")
        offset = int(page_token) if page_token else 0

        all_experiments = list(state.experiments.values())
        slice_exps = all_experiments[offset : offset + max_results]

        resp: dict[str, Any] = {"experiments": slice_exps}
        if offset + max_results < len(all_experiments):
            resp["next_page_token"] = str(offset + max_results)
        return JSONResponse(resp)

    @app.post("/api/2.0/mlflow/runs/search")
    async def search_runs(request: Request) -> Response:
        try:
            body: dict[str, Any] = cast(dict[str, Any], await request.json())
        except Exception:
            body = {}
        experiment_ids: list[str] = cast(list[str], body.get("experiment_ids", []))
        max_results = min(int(body.get("max_results") or state.page_size), state.page_size)
        page_token = body.get("page_token")
        offset = int(page_token) if page_token else 0

        matching_runs = [
            r
            for r in state.runs.values()
            if not experiment_ids or r["info"]["experiment_id"] in experiment_ids
        ]
        # ordered by start time, newest first
        sorted_runs = sorted(
            matching_runs, key=lambda r: int(r["info"].get("start_time", 0)), reverse=True
        )
        slice_runs = sorted_runs[offset : offset + max_results]

        resp: dict[str, Any] = {"runs": slice_runs}
        if offset + max_results < len(sorted_runs):
            resp["next_page_token"] = str(offset + max_results)
        return JSONResponse(resp)

    @app.get("/api/2.0/mlflow/metrics/get-history")
    async def get_metric_history(
        run_id: str = "",
        metric_key: str = "",
        max_results: int | None = None,
        page_token: str | None = None,
    ) -> Response:
        if run_id and run_id not in state.runs:
            return JSONResponse(
                {
                    "error_code": "RESOURCE_DOES_NOT_EXIST",
                    "message": f"Run '{run_id}' not found",
                },
                status_code=404,
            )
        key = (run_id, metric_key)
        points = state.metric_history.get(key, [])
        limit = min(max_results or state.page_size, state.page_size)
        offset = int(page_token) if page_token else 0
        slice_points = points[offset : offset + limit]

        resp: dict[str, Any] = {"metrics": slice_points}
        if offset + limit < len(points):
            resp["next_page_token"] = str(offset + limit)
        return JSONResponse(resp)

    @app.get("/api/2.0/mlflow/registered-models/search")
    async def search_registered_models(
        max_results: int | None = None, page_token: str | None = None
    ) -> Response:
        limit = min(max_results or state.page_size, state.page_size)
        offset = int(page_token) if page_token else 0

        models: list[dict[str, Any]] = []
        for name, data in state.registered_models.items():
            model_dict: dict[str, Any] = {"name": name, **data}
            if state.aliases_supported:
                alias_list = [
                    {"alias": a, "version": v} for a, v in state.aliases.get(name, {}).items()
                ]
                model_dict["aliases"] = alias_list
            models.append(model_dict)

        slice_models = models[offset : offset + limit]
        resp: dict[str, Any] = {"registered_models": slice_models}
        if offset + limit < len(models):
            resp["next_page_token"] = str(offset + limit)
        return JSONResponse(resp)

    @app.get("/api/2.0/mlflow/model-versions/search")
    async def search_model_versions(
        filter: str | None = None,
        max_results: int | None = None,
        page_token: str | None = None,
    ) -> Response:
        limit = min(max_results or state.page_size, state.page_size)
        offset = int(page_token) if page_token else 0

        target_model: str | None = None
        if filter:
            match = re.search(r"name\s*=\s*'([^']+)'", filter)
            if match:
                target_model = match.group(1)

        all_versions: list[dict[str, Any]] = []
        if target_model is not None:
            all_versions.extend(state.model_versions.get(target_model, []))
        else:
            for versions in state.model_versions.values():
                all_versions.extend(versions)

        # Sort newest first
        sorted_versions = sorted(
            all_versions, key=lambda v: int(v.get("creation_timestamp", 0)), reverse=True
        )
        slice_versions = sorted_versions[offset : offset + limit]

        resp: dict[str, Any] = {"model_versions": slice_versions}
        if offset + limit < len(sorted_versions):
            resp["next_page_token"] = str(offset + limit)
        return JSONResponse(resp)

    @app.get("/api/2.0/mlflow/registered-models/alias")
    async def get_model_version_by_alias(name: str = "", alias: str = "") -> Response:
        model_aliases = state.aliases.get(name, {})
        version_str = model_aliases.get(alias)
        if version_str is None:
            return JSONResponse(
                {
                    "error_code": "RESOURCE_DOES_NOT_EXIST",
                    "message": f"Registered model alias '{alias}' not found for model '{name}'",
                },
                status_code=404,
            )

        versions = state.model_versions.get(name, [])
        for v in versions:
            if v["version"] == version_str:
                return JSONResponse({"model_version": v})

        # Version record not found
        return JSONResponse(
            {
                "error_code": "RESOURCE_DOES_NOT_EXIST",
                "message": f"Version '{version_str}' not found for model '{name}'",
            },
            status_code=404,
        )

    @app.get("/get-artifact")
    async def get_artifact(run_uuid: str = "", run_id: str = "", path: str = "") -> Response:
        run_key = run_uuid or run_id
        content = state.artifacts.get((run_key, path))
        if content is None:
            return JSONResponse(
                {
                    "error_code": "RESOURCE_DOES_NOT_EXIST",
                    "message": f"Artifact not found for run '{run_key}' and path '{path}'",
                },
                status_code=404,
            )
        return Response(content=content, media_type="application/octet-stream")

    return app


@dataclass
class RunningFakeMlflow:
    base_url: str
    state: FakeMlflowState
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_fake_mlflow(state: FakeMlflowState, port: int = 0) -> RunningFakeMlflow:
    port = port or _free_port()
    config = uvicorn.Config(
        create_fake_mlflow_app(state), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the fake MLflow server did not start within 10 seconds")
        time.sleep(0.02)
    return RunningFakeMlflow(f"http://127.0.0.1:{port}", state, server, thread)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake MLflow REST server for tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_fake_mlflow_app(FakeMlflowState()),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
