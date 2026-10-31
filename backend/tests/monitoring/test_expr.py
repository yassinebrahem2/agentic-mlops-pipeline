"""Tests for the condition expression language (I23)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mlops_agents.monitoring.expr import (
    AllOf,
    AnyOf,
    Compare,
    Not,
    evaluate,
)


def test_evaluate_operators_numbers() -> None:
    row = {"val": 10.0}
    assert evaluate(Compare(op="lt", field="val", value=15.0), row) is True
    assert evaluate(Compare(op="lt", field="val", value=10.0), row) is False
    assert evaluate(Compare(op="le", field="val", value=10.0), row) is True
    assert evaluate(Compare(op="le", field="val", value=9.0), row) is False
    assert evaluate(Compare(op="gt", field="val", value=5.0), row) is True
    assert evaluate(Compare(op="gt", field="val", value=10.0), row) is False
    assert evaluate(Compare(op="ge", field="val", value=10.0), row) is True
    assert evaluate(Compare(op="ge", field="val", value=11.0), row) is False
    assert evaluate(Compare(op="eq", field="val", value=10.0), row) is True
    assert evaluate(Compare(op="eq", field="val", value=9.0), row) is False
    assert evaluate(Compare(op="ne", field="val", value=9.0), row) is True
    assert evaluate(Compare(op="ne", field="val", value=10.0), row) is False


def test_evaluate_operators_strings_and_booleans() -> None:
    row = {"status": "ok", "active": True}
    assert evaluate(Compare(op="eq", field="status", value="ok"), row) is True
    assert evaluate(Compare(op="ne", field="status", value="fail"), row) is True
    assert evaluate(Compare(op="eq", field="active", value=True), row) is True
    assert evaluate(Compare(op="ne", field="active", value=False), row) is True


def test_evaluate_all_of_any_of_not() -> None:
    row = {"a": 5, "b": 10}
    c1 = Compare(op="gt", field="a", value=0)
    c2 = Compare(op="lt", field="b", value=20)
    c3 = Compare(op="gt", field="b", value=50)

    # all_of
    assert evaluate(AllOf(all_of=[c1, c2]), row) is True
    assert evaluate(AllOf(all_of=[c1, c3]), row) is False
    assert evaluate(AllOf(all_of=[]), row) is True

    # any_of
    assert evaluate(AnyOf(any_of=[c1, c3]), row) is True
    assert evaluate(AnyOf(any_of=[c3]), row) is False
    assert evaluate(AnyOf(any_of=[]), row) is False

    # not_
    assert evaluate(Not(not_=c1), row) is False
    assert evaluate(Not(not_=c3), row) is True


def test_evaluate_missing_field() -> None:
    row = {"a": 5}
    assert evaluate(Compare(op="eq", field="missing", value=5), row) is False
    assert evaluate(Compare(op="ne", field="missing", value=5), row) is False
    assert evaluate(Compare(op="gt", field="missing", value=0), row) is False


def test_evaluate_detail_access() -> None:
    row = {"detail": {"accuracy": 0.95, "nested": "yes"}}
    assert evaluate(Compare(op="gt", field="detail.accuracy", value=0.9), row) is True
    assert evaluate(Compare(op="lt", field="detail.accuracy", value=0.9), row) is False
    assert evaluate(Compare(op="eq", field="detail.nested", value="yes"), row) is True
    # Missing key in detail
    assert evaluate(Compare(op="eq", field="detail.missing", value="yes"), row) is False
    # Missing detail dict
    assert evaluate(Compare(op="eq", field="detail.accuracy", value=0.95), {}) is False


def test_evaluate_mixed_types() -> None:
    # String in row, number in value
    row_str = {"val": "10"}
    assert evaluate(Compare(op="lt", field="val", value=15.0), row_str) is False
    assert evaluate(Compare(op="gt", field="val", value=5.0), row_str) is False
    assert evaluate(Compare(op="eq", field="val", value=10.0), row_str) is False
    assert evaluate(Compare(op="ne", field="val", value=10.0), row_str) is False

    # Number in row, string in value
    row_num = {"val": 10.0}
    assert evaluate(Compare(op="eq", field="val", value="10"), row_num) is False
    assert evaluate(Compare(op="ne", field="val", value="10"), row_num) is False

    # None in row
    row_none = {"val": None}
    assert evaluate(Compare(op="eq", field="val", value=10.0), row_none) is False
    assert evaluate(Compare(op="ne", field="val", value=10.0), row_none) is False

    # Bool in row, number in value (bool must not be treated as int)
    row_bool = {"val": True}
    assert evaluate(Compare(op="eq", field="val", value=1.0), row_bool) is False
    assert evaluate(Compare(op="ne", field="val", value=1.0), row_bool) is False


def test_condition_validation_depth_limits() -> None:
    c = Compare(op="eq", field="x", value=1)
    # depth 1: c
    # depth 2: Not(c)
    # depth 3: Not(Not(c))
    # depth 4: Not(Not(Not(c))) - allowed
    d2 = Not(not_=c)
    d3 = Not(not_=d2)
    d4 = Not(not_=d3)
    assert d4 is not None

    # depth 5: Not(d4) - refused
    with pytest.raises(ValidationError, match=r"[Dd]epth"):
        Not(not_=d4)


def test_condition_validation_node_count_limits() -> None:
    c = Compare(op="eq", field="x", value=1)
    # 1 AllOf + 31 Compares = 32 nodes - allowed
    a32 = AllOf(all_of=[c] * 31)
    assert a32 is not None

    # 1 AllOf + 32 Compares = 33 nodes - refused
    with pytest.raises(ValidationError, match=r"[Nn]ode"):
        AllOf(all_of=[c] * 32)
