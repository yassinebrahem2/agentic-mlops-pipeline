from typing import Any


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
