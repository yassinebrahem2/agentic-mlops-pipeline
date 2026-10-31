"""Expression language for monitoring results (I32): syntax, limits and evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, Self, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

MAX_CONDITION_DEPTH: Final = 4
MAX_CONDITION_NODES: Final = 32


class Compare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["lt", "le", "gt", "ge", "eq", "ne"]
    field: str
    value: float | str | bool


class AllOf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_of: list[Condition]

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        _validate_limits(self)
        return self


class AnyOf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    any_of: list[Condition]

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        _validate_limits(self)
        return self


class Not(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_: Condition

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        _validate_limits(self)
        return self


ConditionNode = Compare | AllOf | AnyOf | Not


def _depth(c: ConditionNode) -> int:
    if isinstance(c, Compare):
        return 1
    if isinstance(c, (AllOf, AnyOf)):
        sub = c.all_of if isinstance(c, AllOf) else c.any_of
        return 1 + (max((_depth(x) for x in sub), default=0))
    return 1 + _depth(c.not_)


def _node_count(c: ConditionNode) -> int:
    if isinstance(c, Compare):
        return 1
    if isinstance(c, (AllOf, AnyOf)):
        sub = c.all_of if isinstance(c, AllOf) else c.any_of
        return 1 + sum(_node_count(x) for x in sub)
    return 1 + _node_count(c.not_)


def _validate_limits(c: ConditionNode) -> ConditionNode:
    depth = _depth(c)
    if depth > MAX_CONDITION_DEPTH:
        raise ValueError(f"Condition depth {depth} exceeds maximum of {MAX_CONDITION_DEPTH}")
    nodes = _node_count(c)
    if nodes > MAX_CONDITION_NODES:
        raise ValueError(f"Condition node count {nodes} exceeds maximum of {MAX_CONDITION_NODES}")
    return c


Condition = Annotated[
    Compare | AllOf | AnyOf | Not,
    AfterValidator(_validate_limits),
]


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def evaluate(cond: Condition, row: Mapping[str, Any]) -> bool:
    """Pure evaluate function for Condition over a row dict."""
    if isinstance(cond, AllOf):
        return all(evaluate(sub, row) for sub in cond.all_of)
    if isinstance(cond, AnyOf):
        return any(evaluate(sub, row) for sub in cond.any_of)
    if isinstance(cond, Not):
        return not evaluate(cond.not_, row)

    field = cond.field
    field_val: object
    if field.startswith("detail."):
        detail_key = field[len("detail.") :]
        detail_obj = row.get("detail")
        if not isinstance(detail_obj, Mapping) or detail_key not in detail_obj:
            return False
        field_val = cast(object, detail_obj[detail_key])
    else:
        if field not in row:
            return False
        field_val = cast(object, row[field])

    target_val = cond.value

    if _is_number(field_val) and _is_number(target_val):
        f_num = float(cast(int | float, field_val))
        t_num = float(cast(int | float, target_val))
        match cond.op:
            case "lt":
                return f_num < t_num
            case "le":
                return f_num <= t_num
            case "gt":
                return f_num > t_num
            case "ge":
                return f_num >= t_num
            case "eq":
                return f_num == t_num
            case "ne":
                return f_num != t_num
    if isinstance(field_val, str) and isinstance(target_val, str):
        match cond.op:
            case "lt":
                return field_val < target_val
            case "le":
                return field_val <= target_val
            case "gt":
                return field_val > target_val
            case "ge":
                return field_val >= target_val
            case "eq":
                return field_val == target_val
            case "ne":
                return field_val != target_val
    if isinstance(field_val, bool) and isinstance(target_val, bool):
        if cond.op == "eq":
            return field_val == target_val
        if cond.op == "ne":
            return field_val != target_val
        if cond.op == "lt":
            return (not field_val) and target_val
        if cond.op == "le":
            return (not field_val) or target_val
        if cond.op == "gt":
            return field_val and (not target_val)
        if cond.op == "ge":
            return field_val or (not target_val)
    return False
