"""MLflow evaluation artifact fetcher."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import httpx2

from mlops_agents.credentials.scrub import error_text
from mlops_agents.tracking.adapters.mlflow_client import SyncAbortError
from mlops_agents.tracking.contract import (
    EvaluationRecord,
    EvaluationStatus,
    ModelVersionRow,
    TrackingSourceView,
)

logger = logging.getLogger(__name__)

MLFLOW_EVAL_WINDOW: Final[timedelta] = timedelta(days=7)
MLFLOW_FETCH_CONCURRENCY: Final[int] = 8
EVALUATION_MAX_BYTES: Final[int] = 262144  # 256 KB


async def fetch_one_evaluation(
    http: httpx2.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    run_key: str,
    sem: asyncio.Semaphore,
) -> tuple[EvaluationStatus, dict[str, Any] | None, bool]:
    """Fetch and validate a single evaluation artifact for a run."""
    async with sem:
        try:
            async with http.stream(
                "GET",
                f"{base_url}/get-artifact",
                headers=headers,
                params={"run_uuid": run_key, "path": "mlops_agents/evaluation.json"},
                follow_redirects=False,
                timeout=30.0,
            ) as resp:
                if resp.status_code in (401, 403):
                    raise SyncAbortError("The tracker rejected the credential")
                if resp.status_code == 404:
                    return "absent", None, False
                if resp.status_code != 200:
                    return "fetch_failed", None, True

                body_bytes = bytearray()
                async for chunk in resp.aiter_bytes():
                    body_bytes.extend(chunk)
                    if len(body_bytes) > EVALUATION_MAX_BYTES:
                        return "fetch_failed", None, True

        except httpx2.HTTPError as exc:
            raise SyncAbortError(error_text(exc)) from exc

        try:
            raw_payload: Any = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return "fetch_failed", None, True

        if isinstance(raw_payload, dict):
            payload = cast(dict[str, Any], raw_payload)
            sv = payload.get("schema_version")
            if isinstance(sv, int) and not isinstance(sv, bool):
                return "present", payload, False

        return "fetch_failed", None, True


async def fetch_evaluations(
    http: httpx2.AsyncClient,
    source: TrackingSourceView,
    base_url: str,
    headers: dict[str, str],
    aliases_map: dict[str, dict[str, str]],
    versions_by_model: dict[str, list[dict[str, Any]]],
    sync_time: datetime,
) -> tuple[list[ModelVersionRow], list[EvaluationRecord]]:
    """Fetch evaluations concurrently for versions and build version rows."""
    sem = asyncio.Semaphore(MLFLOW_FETCH_CONCURRENCY)
    skipped_count = 0

    items_to_fetch: list[tuple[str, str, str | None, int, dict[str, Any]]] = []
    for m_name, v_list in versions_by_model.items():
        for v in v_list:
            v_str = str(v.get("version", ""))
            r_key = str(v.get("run_id", "")).strip() or None
            ts_ms = int(v.get("creation_timestamp") or 0)
            items_to_fetch.append((m_name, v_str, r_key, ts_ms, v))

    # Concurrently fetch artifacts using TaskGroup so any 401/error cancels siblings
    results_map: dict[str, tuple[EvaluationStatus, dict[str, Any] | None, bool]] = {}

    run_keys_to_fetch = {r_key for _, _, r_key, _, _ in items_to_fetch if r_key is not None}

    async def _fetch_task(r_k: str) -> None:
        res = await fetch_one_evaluation(http, base_url, headers, r_k, sem)
        results_map[r_k] = res

    if run_keys_to_fetch:
        try:
            async with asyncio.TaskGroup() as tg:
                for r_k in run_keys_to_fetch:
                    tg.create_task(_fetch_task(r_k))
        except* SyncAbortError as eg:
            raise eg.exceptions[0] from None

    version_rows: list[ModelVersionRow] = []
    eval_records: list[EvaluationRecord] = []

    for m_name, v_str, r_key, ts_ms, _ in items_to_fetch:
        created_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        m_aliases = aliases_map.get(m_name, {})
        v_aliases = sorted([a for a, ver in m_aliases.items() if ver == v_str])

        evaluation_status: EvaluationStatus
        if r_key is None:
            evaluation_status = "absent"
        else:
            evaluation_status, payload, skipped = results_map[r_key]
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
                evaluation=evaluation_status,
            )
        )

    if skipped_count > 0:
        logger.warning(
            "Skipped %d evaluation artifact(s) that could not be read or parsed",
            skipped_count,
        )

    return version_rows, eval_records
