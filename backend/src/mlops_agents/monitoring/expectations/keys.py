"""Expectation key builders and mappings from results, checks, and sources."""

from __future__ import annotations

from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.results.contract import StoredResult, parse_source_id


def feed_key(ref: str, model: str | None = None) -> ExpectationKey:
    """Builds an ExpectationKey for a data feed."""
    return ExpectationKey(kind="feed", ref=ref, model=model)


def job_key(ref: str, model: str | None = None) -> ExpectationKey:
    """Builds an ExpectationKey for an operational job feed."""
    return ExpectationKey(kind="job", ref=ref, model=model)


def check_key(check_id: str, model: str | None = None) -> ExpectationKey:
    """Builds an ExpectationKey for a check."""
    return ExpectationKey(kind="check", ref=check_id, model=model)


def training_request_key(request_id: str, model: str | None = None) -> ExpectationKey:
    """Builds an ExpectationKey for a training request."""
    return ExpectationKey(kind="training_request", ref=request_id, model=model)


def key_from_check_id(check_id: str, model: str | None = None) -> ExpectationKey:
    """Maps a check_id to an ExpectationKey of kind check."""
    return check_key(check_id, model=model)


def key_from_source_id(source_id: str, model: str | None = None) -> ExpectationKey | None:
    """Maps a source_id (e.g. ci:<workflow>) to an ExpectationKey if applicable."""
    try:
        kind, rest, _ = parse_source_id(source_id)
    except ValueError:
        return None

    if kind == "ci":
        return job_key(rest, model=model)
    return None


def keys_for_result(result: StoredResult) -> list[ExpectationKey]:
    """Derives candidate ExpectationKeys that a stored result may satisfy."""
    keys: list[ExpectationKey] = []

    def _add(k: ExpectationKey | None) -> None:
        if k is not None and k not in keys:
            keys.append(k)

    # 1. From check_id as a check
    _add(check_key(result.check_id, result.model))
    if result.model is not None:
        _add(check_key(result.check_id, None))

    # 2. From check_id as a job
    _add(job_key(result.check_id, result.model))
    if result.model is not None:
        _add(job_key(result.check_id, None))

    # 3. From check_id as a feed
    _add(feed_key(result.check_id, result.model))
    if result.model is not None:
        _add(feed_key(result.check_id, None))

    # 4. From source_id
    src_key = key_from_source_id(result.source_id, result.model)
    if src_key:
        _add(src_key)
        if result.model is not None:
            _add(job_key(src_key.ref, None))

    # 5. From subject if distinct
    if result.subject != result.check_id:
        _add(check_key(result.subject, result.model))
        _add(job_key(result.subject, result.model))
        _add(feed_key(result.subject, result.model))
        if result.model is not None:
            _add(check_key(result.subject, None))
            _add(job_key(result.subject, None))
            _add(feed_key(result.subject, None))

    # 6. From source_ref
    if result.source_ref and result.source_ref not in (result.check_id, result.subject):
        _add(job_key(result.source_ref, result.model))
        _add(check_key(result.source_ref, result.model))
        if result.model is not None:
            _add(job_key(result.source_ref, None))
            _add(check_key(result.source_ref, None))

    return keys
