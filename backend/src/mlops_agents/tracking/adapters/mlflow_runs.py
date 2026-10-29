"""MLflow experiments, runs, and metrics fetching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from mlops_agents.tracking.adapters.mlflow_client import MlflowClient
from mlops_agents.tracking.contract import (
    MetricPoint,
    RunRow,
    RunStatus,
    TrackingSourceView,
)

MLFLOW_RUN_CAP: Final[int] = 500
MLFLOW_OVERLAP: Final[timedelta] = timedelta(days=1)
MLFLOW_SERIES_PAGES: Final[int] = 10


async def fetch_experiments(
    client: MlflowClient, base_url: str, headers: dict[str, str]
) -> dict[str, str]:
    """Fetch all experiments and map ID to name."""
    experiments: dict[str, str] = {}
    page_token: str | None = None
    while True:
        body: dict[str, Any] = {"max_results": 100}
        if page_token:
            body["page_token"] = page_token
        data = await client.request(
            "POST", f"{base_url}/api/2.0/mlflow/experiments/search", headers, json_body=body
        )
        for exp in data.get("experiments", []):
            exp_id = str(exp.get("experiment_id", ""))
            if exp_id:
                experiments[exp_id] = str(exp.get("name", exp_id))
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return experiments


async def fetch_runs(
    client: MlflowClient,
    source: TrackingSourceView,
    base_url: str,
    headers: dict[str, str],
    experiments: dict[str, str],
    since_utc: datetime | None,
) -> tuple[list[RunRow], set[str], dict[str, list[str]]]:
    """Fetch up to MLFLOW_RUN_CAP newest runs across experiments."""
    if not experiments:
        return [], set(), {}

    all_runs_data: list[dict[str, Any]] = []
    page_token: str | None = None
    experiment_ids = list(experiments.keys())

    while len(all_runs_data) < MLFLOW_RUN_CAP:
        max_results = min(100, MLFLOW_RUN_CAP - len(all_runs_data))
        body: dict[str, Any] = {
            "experiment_ids": experiment_ids,
            "max_results": max_results,
        }
        if page_token:
            body["page_token"] = page_token
        data = await client.request(
            "POST", f"{base_url}/api/2.0/mlflow/runs/search", headers, json_body=body
        )
        runs_slice = data.get("runs", [])
        all_runs_data.extend(runs_slice)
        page_token = data.get("next_page_token")
        if not page_token or not runs_slice:
            break

    run_rows: list[RunRow] = []
    changed_run_keys: set[str] = set()
    run_metric_keys: dict[str, list[str]] = {}

    for item in all_runs_data[:MLFLOW_RUN_CAP]:
        info = item.get("info", {})
        data = item.get("data", {})
        inputs = item.get("inputs", {})

        run_id = str(info.get("run_id") or info.get("run_uuid") or "")
        if not run_id:
            continue

        exp_id = str(info.get("experiment_id", ""))
        exp_name = experiments.get(exp_id, exp_id)

        raw_status = str(info.get("status") or "").upper()
        status: RunStatus
        if raw_status in ("RUNNING", "SCHEDULED"):
            status = "running"
        elif raw_status == "FINISHED":
            status = "succeeded"
        elif raw_status == "FAILED":
            status = "failed"
        elif raw_status == "KILLED":
            status = "cancelled"
        else:
            status = "other"

        start_ms = info.get("start_time")
        start_dt = (
            datetime.fromtimestamp(start_ms / 1000.0, tz=UTC) if start_ms is not None else None
        )

        end_ms = info.get("end_time")
        end_dt = datetime.fromtimestamp(end_ms / 1000.0, tz=UTC) if end_ms is not None else None

        if (
            since_utc is None
            or status == "running"
            or (end_dt is not None and end_dt >= (since_utc - MLFLOW_OVERLAP))
        ):
            changed_run_keys.add(run_id)

        git_commit: str | None = None
        tags_dict: dict[str, str] = {}
        tags_list: list[dict[str, Any]] = cast(list[dict[str, Any]], data.get("tags") or [])
        for t in tags_list:
            key = str(t.get("key", ""))
            val = str(t.get("value", ""))[:500]
            if key == "mlflow.source.git.commit":
                git_commit = val
            if len(tags_dict) < 100:
                tags_dict[key] = val

        params_dict: dict[str, str] = {}
        params_list: list[dict[str, Any]] = cast(list[dict[str, Any]], data.get("params") or [])
        for p in params_list:
            key = str(p.get("key", ""))
            val = str(p.get("value", ""))[:500]
            params_dict[key] = val

        datasets: list[str] = []
        dataset_inputs: list[dict[str, Any]] = cast(
            list[dict[str, Any]], inputs.get("dataset_inputs") or []
        )
        for inp in dataset_inputs:
            ds: dict[str, Any] = cast(dict[str, Any], inp.get("dataset") or {})
            ds_name = ds.get("name")
            if ds_name and ds_name not in datasets:
                datasets.append(str(ds_name))

        metrics_list: list[dict[str, Any]] = cast(list[dict[str, Any]], data.get("metrics") or [])
        run_metric_keys[run_id] = [str(m.get("key")) for m in metrics_list if m.get("key")]

        run_rows.append(
            RunRow(
                source_id=source.id,
                key=run_id,
                experiment=exp_name,
                status=status,
                start=start_dt,
                end=end_dt,
                params=params_dict,
                tags=tags_dict,
                git_commit=git_commit,
                datasets=datasets,
            )
        )

    return run_rows, changed_run_keys, run_metric_keys


async def fetch_metrics(
    client: MlflowClient,
    source: TrackingSourceView,
    base_url: str,
    headers: dict[str, str],
    runs: list[RunRow],
    changed_run_keys: set[str],
    run_metric_keys: dict[str, list[str]],
) -> list[MetricPoint]:
    """Fetch metric history for changed runs."""
    metric_points: list[MetricPoint] = []

    for run in runs:
        if run.key not in changed_run_keys:
            continue

        metric_keys: list[str] = run_metric_keys.get(run.key, [])
        for m_key in metric_keys:
            page_token: str | None = None
            pages_read = 0

            while pages_read < MLFLOW_SERIES_PAGES:
                params: dict[str, Any] = {
                    "run_id": run.key,
                    "metric_key": m_key,
                    "max_results": 1000,
                }
                if page_token:
                    params["page_token"] = page_token

                data = await client.request(
                    "GET",
                    f"{base_url}/api/2.0/mlflow/metrics/get-history",
                    headers,
                    params=params,
                )
                points = data.get("metrics", [])
                for pt in points:
                    ts_ms = int(pt.get("timestamp") or 0)
                    pt_time = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
                    metric_points.append(
                        MetricPoint(
                            source_id=source.id,
                            run_key=run.key,
                            metric=m_key,
                            step=int(pt.get("step") or 0),
                            value=float(pt.get("value") or 0.0),
                            time=pt_time,
                        )
                    )

                pages_read += 1
                page_token = data.get("next_page_token")
                if not page_token or not points:
                    break

    return metric_points
