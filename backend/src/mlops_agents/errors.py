from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

_VALUE_ERROR_PREFIX = "Value error, "


class DomainError(Exception):
    """An error a user can act on. `message` is shown to them as is."""

    def __init__(
        self, code: str, message: str, *, status: int = 400, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


class _ErrorList(Protocol):
    """FastAPI's `RequestValidationError`, without importing the web layer here."""

    def errors(self) -> Sequence[Any]: ...


@dataclass(frozen=True)
class ValidationErrorMessage:
    """A validation error as locations and sentences only."""

    errors: list[dict[str, str]]  # each {"field", "message"}, the 422 body's `detail.errors`

    @property
    def text(self) -> str:
        return "; ".join(f"{e['field']}: {e['message']}" for e in self.errors)


def validation_error_message(err: ValidationError | _ErrorList) -> ValidationErrorMessage:
    """The one way a validation error reaches a user or a log: the submitted input may hold
    a key, so only each error's location and sentence are kept."""
    if isinstance(err, ValidationError):
        raw = err.errors(include_input=False, include_url=False)
    else:
        raw = err.errors()
    return ValidationErrorMessage(
        errors=[
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": str(error["msg"]).removeprefix(_VALUE_ERROR_PREFIX),
            }
            for error in raw
        ]
    )
