"""CommandSpec: the command and ci_dispatch shapes and the overview's template grammar."""

from typing import Any

import pytest
from pydantic import ValidationError

from mlops_agents.proposals.payloads import CommandSpec
from tests.proposals.samples import ci_dispatch, command, error_text


def test_both_command_types_pass() -> None:
    CommandSpec.model_validate(command())
    CommandSpec.model_validate(ci_dispatch())
    CommandSpec.model_validate(ci_dispatch(inputs={}, placeholders=[]))


@pytest.mark.parametrize(
    "spec",
    [
        command(template=None),
        command(template=""),
        command(template="   "),
        command(workflow="train.yml"),
        command(inputs={"day": "{model}"}),
        ci_dispatch(workflow=None),
        ci_dispatch(workflow=" "),
        ci_dispatch(template="python train.py"),
        ci_dispatch(template=""),
    ],
)
def test_command_shapes_are_rejected(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(spec)


def test_declared_but_unused_placeholders_are_allowed() -> None:
    CommandSpec.model_validate(command(placeholders=["model", "threshold"]))
    CommandSpec.model_validate(ci_dispatch(placeholders=["window_end", "model"]))


@pytest.mark.parametrize(
    "template",
    [
        "run --end {window_end}",  # undeclared
        "run {model!r}",  # conversion
        "run {model!z}",  # unknown conversion
        "run {model:%Y}",  # format spec
        "run {model:>{width}}",  # nested field, width undeclared
        "run {model:>{model}}",  # nested field, even of a declared name
        "run {}",  # positional
        "run {0}",
        "run {model.real}",  # attribute
        "run {model[0]}",  # index
        "run {",  # unmatched braces
        "run }",
        "run {model",
        "run {model!}",
        "echo ${HOME}",  # a shell brace written undoubled
    ],
)
def test_template_grammar_rejects(template: str) -> None:
    with pytest.raises(ValidationError) as err:
        CommandSpec.model_validate(command(template=template))
    text = error_text(err)
    assert "template" in text
    assert "{{" in text
    assert "}}" in text


def test_the_grammar_error_names_the_replacement_field() -> None:
    with pytest.raises(ValidationError) as err:
        CommandSpec.model_validate(command(template="run --end {window_end}"))
    assert "{window_end}" in error_text(err)
    with pytest.raises(ValidationError) as err2:
        CommandSpec.model_validate(command(template="run {model:>{width}}"))
    assert "{model:>{width}}" in error_text(err2)
    with pytest.raises(ValidationError) as err3:
        CommandSpec.model_validate(command(template="run {model!r}"))
    assert "{model!r}" in error_text(err3)


@pytest.mark.parametrize("value", ["{run_date}", "{window_end!r}", "{window_end:%Y}", "{", "a } b"])
def test_ci_dispatch_inputs_follow_the_grammar(value: str) -> None:
    with pytest.raises(ValidationError) as err:
        CommandSpec.model_validate(ci_dispatch(inputs={"day": "{window_end}", "date": value}))
    text = error_text(err)
    assert "'date'" in text
    assert "{{" in text
    assert "}}" in text


@pytest.mark.parametrize(
    "template",
    [
        "echo ${{HOME}} {model}",
        "awk '{{print $1}}' data.csv",
        "python train.py --model {model} --model-again {model}",
        "make train",
    ],
)
def test_literal_braces_and_bare_fields_pass(template: str) -> None:
    CommandSpec.model_validate(command(template=template))


def test_ci_dispatch_input_with_literal_braces_passes() -> None:
    CommandSpec.model_validate(ci_dispatch(inputs={"day": "{window_end}", "raw": "{{x}}"}))
