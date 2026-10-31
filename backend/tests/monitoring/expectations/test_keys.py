"""Tests for expectation key builders and mappings (keys.py)."""

from datetime import UTC, datetime

from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.expectations.keys import (
    check_key,
    feed_key,
    job_key,
    key_from_check_id,
    key_from_source_id,
    keys_for_result,
    training_request_key,
)
from mlops_agents.monitoring.results.contract import StoredResult


def test_key_builders() -> None:
    assert feed_key("daily_raw") == ExpectationKey(kind="feed", ref="daily_raw", model=None)
    assert job_key("eval_job", model="churn") == ExpectationKey(
        kind="job", ref="eval_job", model="churn"
    )
    assert check_key("chk_drift") == ExpectationKey(kind="check", ref="chk_drift", model=None)
    assert training_request_key("tr_123", model="churn") == ExpectationKey(
        kind="training_request", ref="tr_123", model="churn"
    )


def test_key_from_check_id() -> None:
    key = key_from_check_id("chk_features", model="fraud")
    assert key == ExpectationKey(kind="check", ref="chk_features", model="fraud")


def test_key_from_source_id() -> None:
    ci_key = key_from_source_id("ci:eval.yml", model="churn")
    assert ci_key == ExpectationKey(kind="job", ref="eval.yml", model="churn")

    file_key = key_from_source_id("file:raw/*.csv:conn_local")
    assert file_key is None

    invalid_key = key_from_source_id("invalid_source")
    assert invalid_key is None


def test_keys_for_result() -> None:
    now = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    result = StoredResult(
        id="res_1",
        check_id="ci:eval.yml",
        origin="tracker",
        ran_at=now,
        fired=False,
        category="operational",
        subject="eval.yml",
        model="churn",
        detail={},
        source_ref="ci:eval.yml",
        source_id="ci:eval.yml",
        source_key="run_100",
        seen_at=now,
    )
    candidates = keys_for_result(result)
    assert ExpectationKey(kind="check", ref="ci:eval.yml", model="churn") in candidates
    assert ExpectationKey(kind="feed", ref="ci:eval.yml", model="churn") in candidates
    assert ExpectationKey(kind="job", ref="eval.yml", model="churn") in candidates
    assert ExpectationKey(kind="job", ref="eval.yml", model=None) in candidates
