from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from mlops_agents.errors import DomainError, validation_error_message


def test_domain_error_fields() -> None:
    err = DomainError("project_exists", "Already added.", status=409, detail={"id": "prj_1"})
    assert err.code == "project_exists"
    assert err.message == "Already added."
    assert err.status == 409
    assert err.detail == {"id": "prj_1"}
    assert str(err) == "Already added."


class _Thing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str

    @field_validator("url")
    @classmethod
    def https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Use https.")
        return value


def _error(data: dict[str, object]) -> ValidationError:
    try:
        _Thing.model_validate(data)
    except ValidationError as err:
        return err
    raise AssertionError("expected a ValidationError")


def test_validation_error_message_keeps_location_and_sentence_only() -> None:
    secret = "sk-secret-in-input-123"

    message = validation_error_message(_error({"name": "a", "url": "https://x", "key": secret}))

    assert message.errors == [{"field": "key", "message": "Extra inputs are not permitted"}]
    assert message.text == "key: Extra inputs are not permitted"
    assert secret not in repr(message)


def test_validation_error_message_strips_the_value_error_prefix() -> None:
    message = validation_error_message(_error({"name": "a", "url": "http://sk-secret"}))

    assert message.errors == [{"field": "url", "message": "Use https."}]
    assert "sk-secret" not in repr(message)


def test_validation_error_message_joins_several_errors() -> None:
    message = validation_error_message(_error({"url": "http://x"}))

    assert message.text == "name: Field required; url: Use https."


def test_validation_error_message_reads_a_request_validation_error() -> None:
    secret = "sk-secret-in-body-456"
    err = RequestValidationError(
        [{"type": "missing", "loc": ("body", "name"), "msg": "Field required", "input": secret}]
    )

    message = validation_error_message(err)

    assert message.errors == [{"field": "body.name", "message": "Field required"}]
    assert secret not in repr(message)
