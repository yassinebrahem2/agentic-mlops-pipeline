"""MLflow tracking adapter (I16).

Reads experiments, runs, metric series, registered models, versions and evaluation
artifacts from an MLflow tracking server using REST. No MLflow package dependency.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import httpx2

from mlops_agents.credentials.scrub import error_text
from mlops_agents.tracking.contract import (
    EvaluationRecord,
    MetricPoint,
    ModelVersionRow,
    RunRow,
    RunStatus,
    SourceType,
    SyncResult,
    TrackingAdapter,
    TrackingSourceView,
)

logger = logging.getLogger(__name__)

MLFLOW_RUN_CAP: Final[int] = 500
MLFLOW_OVERLAP: Final[timedelta] = timedelta(days=1)
MLFLOW_SERIES_PAGES: Final[int] = 10
MLFLOW_VERSION_CAP: Final[int] = 1000
MLFLOW_EVAL_WINDOW: Final[timedelta] = timedelta(days=7)
MLFLOW_FETCH_CONCURRENCY: Final[int] = 8
EVALUATION_MAX_BYTES: Final[int] = 262144  # 256 KB


class _SyncAbortError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MlflowAdapter(TrackingAdapter):
    type: SourceType = "mlflow"

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def sync(
        self, source: TrackingSourceView, credential: str | None, since: datetime | None
    ) -> SyncResult:
        base_url = str(source.settings.get("url", "")).rstrip("/")
        headers: dict[str, str] = {}
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"

        sync_time = datetime.now(UTC)
        since_utc = (
            since if (since is None or since.tzinfo is not None) else since.replace(tzinfo=UTC)
        )

        try:
            return await self._do_sync(source, base_url, headers, since_utc, sync_time)
        except _SyncAbortError as exc:
            return SyncResult(
                versions=[],
                runs=[],
                metrics=[],
                aliases={},
                ci_runs=[],
                evaluations=[],
                error=exc.message,
            )

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            resp = await self._http.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                follow_redirects=False,
                timeout=30.0,
            )
        except httpx2.HTTPError as exc:
            raise _SyncAbortError(error_text(exc)) from exc

        if resp.status_code in (401, 403):
            raise _SyncAbortError("The tracker rejected the credential")
        if resp.status_code != 200:
            raise _SyncAbortError(f"MLflow answered with HTTP {resp.status_code}")

        try:
            return resp.json()
        except Exception as exc:
            raise _SyncAbortError("MLflow answered with data this app cannot read") from exc

    async def _do_sync(
        self,
        source: TrackingSourceView,
        base_url: str,
        headers: dict[str, str],
        since_utc: datetime | None,
        sync_time: datetime,
    ) -> SyncResult:
        # 1. experiments/search (paged) gives experiment ID to name
        experiments = await self._fetch_experiments(base_url, headers)

        # 2. runs/search over all experiment IDs, newest first, at most MLFLOW_RUN_CAP
        runs, changed_run_keys = await self._fetch_runs(
            source, base_url, headers, experiments, since_utc
        )

        # 3. metric history per metric key of changed runs
        metrics = await self._fetch_metrics(source, base_url, headers, runs, changed_run_keys)

        # 4. registered-models/search & model-versions/search
        aliases_map, versions_by_model = await self._fetch_models_and_versions(
            base_url, headers, since_utc
        )

        # 5. evaluations
        version_rows, eval_records = await self._fetch_evaluations(
            source, base_url, headers, aliases_map, versions_by_model, sync_time
        )

        return SyncResult(
            versions=version_rows,
            runs=runs,
            metrics=metrics,
            aliases=aliases_map,
            ci_runs=[],
            evaluations=eval_records,
            error=None,
        )

    async def _fetch_experiments(self, base_url: str, headers: dict[str, str]) -> dict[str, str]:
        experiments: dict[str, str] = {}
        page_token: str | None = None
        while True:
            body: dict[str, Any] = {"max_results": 100}
            if page_token:
                body["page_token"] = page_token
            data = await self._request(
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

    async def _fetch_runs(
        self,
        source: TrackingSourceView,
        base_url: str,
        headers: dict[str, str],
        experiments: dict[str, str],
        since_utc: datetime | None,
    ) -> tuple[list[RunRow], set[str]]:
        if not experiments:
            return [], set()

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
            data = await self._request(
                "POST", f"{base_url}/api/2.0/mlflow/runs/search", headers, json_body=body
            )
            runs_slice = data.get("runs", [])
            all_runs_data.extend(runs_slice)
            page_token = data.get("next_page_token")
            if not page_token or not runs_slice:
                break

        run_rows: list[RunRow] = []
        changed_run_keys: set[str] = set()

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

            # A run is changed when status is running, end_time >= since - MLFLOW_OVERLAP, or since is None
            if (
                since_utc is None
                or status == "running"
                or (end_dt is not None and end_dt >= (since_utc - MLFLOW_OVERLAP))
            ):
                changed_run_keys.add(run_id)

            # Git commit from mlflow.source.git.commit tag
            git_commit: str | None = None
            tags_dict: dict[str, str] = {}
            for t in data.get("tags", []):
                key = str(t.get("key", ""))
                val = str(t.get("value", ""))[:500]
                if key == "mlflow.source.git.commit":
                    git_commit = val
                if len(tags_dict) < 100:
                    tags_dict[key] = val

            # Params
            params_dict: dict[str, str] = {}
            for p in data.get("params", []):
                key = str(p.get("key", ""))
                val = str(p.get("value", ""))[:500]
                params_dict[key] = val

            # Datasets
            datasets: list[str] = []
            for inp in inputs.get("dataset_inputs", []):
                ds = inp.get("dataset", {})
                ds_name = ds.get("name")
                if ds_name and ds_name not in datasets:
                    datasets.append(str(ds_name))

            # Store metrics list on item for step 3
            item["_parsed_metric_keys"] = [
                str(m.get("key")) for m in data.get("metrics", []) if m.get("key")
            ]

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

        # Cache parsed raw runs for metric extraction
        self._last_raw_runs = {
            str(r.get("info", {}).get("run_id") or r.get("info", {}).get("run_uuid")): r
            for r in all_runs_data
        }

        return run_rows, changed_run_keys

    async def _fetch_metrics(
        self,
        source: TrackingSourceView,
        base_url: str,
        headers: dict[str, str],
        runs: list[RunRow],
        changed_run_keys: set[str],
    ) -> list[MetricPoint]:
        metric_points: list[MetricPoint] = []
        raw_runs = getattr(self, "_last_raw_runs", {})

        for run in runs:
            if run.key not in changed_run_keys:
                continue

            raw_run = raw_runs.get(run.key)
            if not raw_run:
                continue

            metric_keys: list[str] = raw_run.get("_parsed_metric_keys", [])
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

                    data = await self._request(
                        "GET",
                        f"{base_url}/api/2.0/mlflow/metrics/get-history",
                        headers,
                        params=params,
                    )
                    points = data.get("metrics", [])
                    for pt in points:
                        ts_ms = pt.get("timestamp", 0)
                        pt_time = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
                        metric_points.append(
                            MetricPoint(
                                source_id=source.id,
                                run_key=run.key,
                                metric=m_key,
                                step=int(pt.get("step", 0)),
                                value=float(pt.get("value", 0.0)),
                                time=pt_time,
                            )
                        )

                    pages_read += 1
                    page_token = data.get("next_page_token")
                    if not page_token or not points:
                        break

        return metric_points

    async def _fetch_models_and_versions(
        self,
        base_url: str,
        headers: dict[str, str],
        since_utc: datetime | None,
    ) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, Any]]]]:
        # registered-models/search (paged)
        aliases_map: dict[str, dict[str, str]] = {}
        model_names: list[str] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {"max_results": 100}
            if page_token:
                params["page_token"] = page_token
            data = await self._request(
                "GET",
                f"{base_url}/api/2.0/mlflow/registered-models/search",
                headers,
                params=params,
            )
            for m in data.get("registered_models", []):
                name = str(m.get("name", ""))
                if not name:
                    continue
                model_names.append(name)
                # Parse aliases
                model_aliases: dict[str, str] = {}
                for alias_item in m.get("aliases", []):
                    a_name = str(alias_item.get("alias", ""))
                    a_ver = str(alias_item.get("version", ""))
                    if a_name and a_ver:
                        model_aliases[a_name] = a_ver
                aliases_map[name] = model_aliases

            page_token = data.get("next_page_token")
            if not page_token:
                break

        # model-versions/search per model
        versions_by_model: dict[str, list[dict[str, Any]]] = {}
        for m_name in model_names:
            m_versions: list[dict[str, Any]] = []
            page_token = None
            while True:
                params = {"filter": f"name='{m_name}'", "max_results": 100}
                if page_token:
                    params["page_token"] = page_token
                data = await self._request(
                    "GET",
                    f"{base_url}/api/2.0/mlflow/model-versions/search",
                    headers,
                    params=params,
                )
                v_slice = data.get("model_versions", [])
                m_versions.extend(v_slice)
                page_token = data.get("next_page_token")
                if not page_token or not v_slice:
                    break

            # Filter versions
            alias_versions = set(aliases_map.get(m_name, {}).values())
            if since_utc is None:
                # Every version, bounded by MLFLOW_VERSION_CAP, newest first
                sorted_vers = sorted(
                    m_versions,
                    key=lambda v: int(v.get("creation_timestamp", 0)),
                    reverse=True,
                )
                versions_by_model[m_name] = sorted_vers[:MLFLOW_VERSION_CAP]
            else:
                # Later those holding an alias plus those created at or after since - MLFLOW_EVAL_WINDOW
                cutoff = since_utc - MLFLOW_EVAL_WINDOW
                filtered_vers: list[dict[str, Any]] = []
                for v in m_versions:
                    v_str = str(v.get("version", ""))
                    ts_ms = int(v.get("creation_timestamp", 0))
                    created_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
                    if v_str in alias_versions or created_dt >= cutoff:
                        filtered_vers.append(v)
                versions_by_model[m_name] = filtered_vers

        return aliases_map, versions_by_model

    async def _fetch_evaluations(
        self,
        source: TrackingSourceView,
        base_url: str,
        headers: dict[str, str],
        aliases_map: dict[str, dict[str, str]],
        versions_by_model: dict[str, list[dict[str, Any]]],
        sync_time: datetime,
    ) -> tuple[list[ModelVersionRow], list[EvaluationRecord]]:
        sem = asyncio.Semaphore(MLFLOW_FETCH_CONCURRENCY)
        skipped_count = 0

        async def fetch_one_evaluation(
            run_key: str,
        ) -> tuple[str, dict[str, Any] | None, bool]:
            async with sem:
                try:
                    resp = await self._http.request(
                        "GET",
                        f"{base_url}/get-artifact",
                        headers=headers,
                        params={"run_uuid": run_key, "path": "mlops_agents/evaluation.json"},
                        follow_redirects=False,
                        timeout=30.0,
                    )
                except httpx2.HTTPError as exc:
                    raise _SyncAbortError(error_text(exc)) from exc

                if resp.status_code in (401, 403):
                    raise _SyncAbortError("The tracker rejected the credential")
                if resp.status_code == 404:
                    return "absent", None, False
                if resp.status_code != 200:
                    return "fetch_failed", None, True

                # Check body length
                if len(resp.content) > EVALUATION_MAX_BYTES:
                    return "fetch_failed", None, True

                try:
                    raw_payload: Any = resp.json()
                except Exception:
                    return "fetch_failed", None, True

                if isinstance(raw_payload, dict):
                    payload = cast(dict[str, Any], raw_payload)
                    sv = payload.get("schema_version")
                    if isinstance(sv, int) and not isinstance(sv, bool):
                        return "present", payload, False

                return "fetch_failed", None, True

        # Build list of items to fetch
        items_to_fetch: list[tuple[str, str, str | None, int, dict[str, Any]]] = []
        for m_name, v_list in versions_by_model.items():
            for v in v_list:
                v_str = str(v.get("version", ""))
                r_key = str(v.get("run_id", "")).strip() or None
                ts_ms = int(v.get("creation_timestamp", 0))
                items_to_fetch.append((m_name, v_str, r_key, ts_ms, v))

        # Run artifact fetches concurrently
        fetch_tasks = [
            fetch_one_evaluation(r_key) if r_key is not None else None
            for _, _, r_key, _, _ in items_to_fetch
        ]

        results: list[tuple[str, dict[str, Any] | None, bool]] = await asyncio.gather(
            *(t for t in fetch_tasks if t is not None), return_exceptions=False
        )

        version_rows: list[ModelVersionRow] = []
        eval_records: list[EvaluationRecord] = []

        result_idx = 0
        for m_name, v_str, r_key, ts_ms, _ in items_to_fetch:
            created_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
            m_aliases = aliases_map.get(m_name, {})
            v_aliases = sorted([a for a, ver in m_aliases.items() if ver == v_str])

            if r_key is None:
                evaluation_status = "absent"
            else:
                evaluation_status, payload, skipped = results[result_idx]
                result_idx += 1
                if skipped:
                    skipped_count += 1
                if evaluation_status == "present" and payload is not None:
                    eval_records.append(
                        EvaluationRecord(
                            source_id=source.id,
                            model=m_name,
                            version=v_str,
                            run_key=r_key,
                            schema_version=int(payload["schema_version"]),
                            payload=payload,
                            fetched_at=sync_time,
                        )
                    )

            version_rows.append(
                ModelVersionRow(
                    source_id=source.id,
                    model=m_name,
                    version=v_str,
                    run_key=r_key,
                    aliases=v_aliases,
                    created=created_dt,
                    evaluation=evaluation_status,  # type: ignore[arg-type]
                )
            )

        if skipped_count > 0:
            logger.warning(
                "Skipped %d evaluation artifact(s) that could not be read or parsed",
                skipped_count,
            )

        return version_rows, eval_records
