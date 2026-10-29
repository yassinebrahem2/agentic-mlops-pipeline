"""MLflow tracking adapter (I16).

Reads experiments, runs, metric series, registered models, versions and evaluation
artifacts from an MLflow tracking server using REST. No MLflow package dependency.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx2

from mlops_agents.tracking.adapters.mlflow_client import (
    MlflowClient,
    SyncAbortError,
)
from mlops_agents.tracking.adapters.mlflow_eval import (
    EVALUATION_MAX_BYTES,
    MLFLOW_EVAL_WINDOW,
    MLFLOW_FETCH_CONCURRENCY,
    fetch_evaluations,
)
from mlops_agents.tracking.adapters.mlflow_runs import (
    MLFLOW_OVERLAP,
    MLFLOW_RUN_CAP,
    MLFLOW_SERIES_PAGES,
    fetch_experiments,
    fetch_metrics,
    fetch_runs,
)
from mlops_agents.tracking.contract import (
    EvaluationStatus,
    SourceType,
    SyncResult,
    TrackingAdapter,
    TrackingSourceView,
)

logger = logging.getLogger(__name__)

MLFLOW_VERSION_CAP: Final[int] = 1000
_SyncAbortError: Final[type[SyncAbortError]] = SyncAbortError

__all__ = [
    "EVALUATION_MAX_BYTES",
    "MLFLOW_EVAL_WINDOW",
    "MLFLOW_FETCH_CONCURRENCY",
    "MLFLOW_OVERLAP",
    "MLFLOW_RUN_CAP",
    "MLFLOW_SERIES_PAGES",
    "MLFLOW_VERSION_CAP",
    "EvaluationStatus",
    "MlflowAdapter",
    "SyncAbortError",
    "_SyncAbortError",
]


class MlflowAdapter(TrackingAdapter):
    type: SourceType = "mlflow"

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http
        self._client = MlflowClient(http)

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._client.request(
            method, url, headers=headers, params=params, json_body=json_body
        )

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
        except SyncAbortError as exc:
            return SyncResult(
                versions=[],
                runs=[],
                metrics=[],
                aliases={},
                ci_runs=[],
                evaluations=[],
                error=exc.message,
            )

    async def _do_sync(
        self,
        source: TrackingSourceView,
        base_url: str,
        headers: dict[str, str],
        since_utc: datetime | None,
        sync_time: datetime,
    ) -> SyncResult:
        # 1. experiments/search (paged) gives experiment ID to name
        experiments = await fetch_experiments(self._client, base_url, headers)

        # 2. runs/search over all experiment IDs, newest first, at most MLFLOW_RUN_CAP
        runs, changed_run_keys, run_metric_keys = await fetch_runs(
            self._client, source, base_url, headers, experiments, since_utc
        )

        # 3. metric history per metric key of changed runs
        metrics = await fetch_metrics(
            self._client, source, base_url, headers, runs, changed_run_keys, run_metric_keys
        )

        # 4. registered-models/search & model-versions/search
        aliases_map, versions_by_model = await self._fetch_models_and_versions(
            base_url, headers, since_utc
        )

        # 5. evaluations
        version_rows, eval_records = await fetch_evaluations(
            self._http,
            source,
            base_url,
            headers,
            aliases_map,
            versions_by_model,
            sync_time,
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

    async def _fetch_models_and_versions(
        self,
        base_url: str,
        headers: dict[str, str],
        since_utc: datetime | None,
    ) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, Any]]]]:
        aliases_map: dict[str, dict[str, str]] = {}
        model_names: list[str] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {"max_results": 100}
            if page_token:
                params["page_token"] = page_token
            data = await self._client.request(
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
                model_aliases: dict[str, str] = {}
                aliases_list: list[dict[str, Any]] = cast(
                    list[dict[str, Any]], m.get("aliases") or []
                )
                for alias_item in aliases_list:
                    a_name = str(alias_item.get("alias", ""))
                    a_ver = str(alias_item.get("version", ""))
                    if a_name and a_ver:
                        model_aliases[a_name] = a_ver
                aliases_map[name] = model_aliases

            page_token = data.get("next_page_token")
            if not page_token:
                break

        versions_by_model: dict[str, list[dict[str, Any]]] = {}
        for m_name in model_names:
            m_versions: list[dict[str, Any]] = []
            page_token = None
            escaped_name = m_name.replace("'", "\\'")
            while True:
                filter_params: dict[str, Any] = {
                    "filter": f"name='{escaped_name}'",
                    "max_results": 100,
                }
                if page_token:
                    filter_params["page_token"] = page_token
                data = await self._client.request(
                    "GET",
                    f"{base_url}/api/2.0/mlflow/model-versions/search",
                    headers,
                    params=filter_params,
                )
                v_slice = data.get("model_versions", [])
                m_versions.extend(v_slice)
                page_token = data.get("next_page_token")
                if not page_token or not v_slice:
                    break

            alias_versions = set(aliases_map.get(m_name, {}).values())
            if since_utc is None:
                sorted_vers = sorted(
                    m_versions,
                    key=lambda v: int(v.get("creation_timestamp") or 0),
                    reverse=True,
                )
                versions_by_model[m_name] = sorted_vers[:MLFLOW_VERSION_CAP]
            else:
                cutoff = since_utc - MLFLOW_EVAL_WINDOW
                filtered_vers: list[dict[str, Any]] = []
                for v in m_versions:
                    v_str = str(v.get("version", ""))
                    ts_ms = int(v.get("creation_timestamp") or 0)
                    created_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
                    if v_str in alias_versions or created_dt >= cutoff:
                        filtered_vers.append(v)
                versions_by_model[m_name] = filtered_vers

        return aliases_map, versions_by_model
