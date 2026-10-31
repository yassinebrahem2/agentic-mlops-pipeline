"""Tests for fill_template in proposals/templates.py."""

from __future__ import annotations

import pytest

from mlops_agents.monitoring.hooks.contract import HookRefused, MissingPlaceholder
from mlops_agents.proposals.templates import fill_template


def test_fill_template_success() -> None:
    template = "python train.py --lr {lr} --model {model_name}"
    values = {"lr": "0.01", "model_name": "resnet"}
    res = fill_template(template, values)
    assert res == "python train.py --lr 0.01 --model resnet"


def test_fill_template_with_literal_braces() -> None:
    template = "echo '{{literal}}' {value}"
    values = {"value": "world"}
    res = fill_template(template, values)
    assert res == "echo '{literal}' world"


def test_fill_template_ignores_extra_values() -> None:
    template = "echo {msg}"
    values = {"msg": "hello", "extra": "ignored"}
    res = fill_template(template, values)
    assert res == "echo hello"


def test_fill_template_raises_missing_placeholder() -> None:
    template = "python train.py --lr {lr} --batch {batch_size}"
    values = {"lr": "0.01"}
    with pytest.raises(MissingPlaceholder) as exc_info:
        fill_template(template, values)

    assert isinstance(exc_info.value, HookRefused)
    assert exc_info.value.placeholder == "batch_size"
    assert "batch_size" in str(exc_info.value)
