"""One JSON error shape for every failure: {"code", "message", "detail"}."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mlops_agents.credentials import KeyStoreError
from mlops_agents.errors import DomainError
from mlops_agents.sandbox.contract import SandboxUnavailable

_log = logging.getLogger(__name__)

_VALUE_ERROR_PREFIX = "Value error, "


def validation_error_message(exc: ValidationError) -> str:
    """Summarizes a ValidationError without including raw input values (which may hold secrets)."""
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc']) or 'payload'}: {str(error['msg']).removeprefix(_VALUE_ERROR_PREFIX)}"
        for error in exc.errors(include_input=False, include_url=False)
    )


def error_response(
    status: int,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"code": code, "message": message, "detail": detail}, status_code=status, headers=headers
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only the location and the sentence: the submitted input may hold a key.
        errors = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": str(error["msg"]).removeprefix(_VALUE_ERROR_PREFIX),
            }
            for error in exc.errors()
        ]
        return error_response(
            422, "invalid_request", "Some fields are missing or invalid.", {"errors": errors}
        )

    @app.exception_handler(KeyStoreError)
    async def keystore_unavailable(request: Request, exc: KeyStoreError) -> JSONResponse:
        return error_response(503, "keystore_unavailable", str(exc))

    @app.exception_handler(SandboxUnavailable)
    async def sandbox_unavailable(request: Request, exc: SandboxUnavailable) -> JSONResponse:
        return error_response(503, "sandbox_unavailable", str(exc))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(
                404, "not_found", "Nothing exists at this address. Check the link.", None
            )
        return error_response(
            exc.status_code, f"http_{exc.status_code}", str(exc.detail), None, exc.headers
        )


class InternalErrors:
    """Turns an unexpected exception into the JSON 500 and logs its type and stack frames.

    Not an exception handler: Starlette's ServerErrorMiddleware re-raises after calling one,
    and the server would then log the raw message and traceback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        finished = False

        async def tracked_send(message: Message) -> None:
            nonlocal started, finished
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                finished = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as exc:
            # The type and frames only: the exception's text can hold a key that error_text
            # does not mask, and exc_info would print that text through the formatter.
            _log.error(
                "Unexpected %s on %s %s\n%s",
                type(exc).__name__,
                scope["method"],
                scope["path"],
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            if not started:
                response = error_response(
                    500,
                    "internal_error",
                    "Something went wrong on the server. The server log has the details.",
                )
                await response(scope, receive, send)
            elif not finished:
                # A stream already sent its headers; all that is left is to end it.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
