"""Serves the built UI, deep links (returning index.html), or a placeholder when not built."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketClose

NOT_BUILT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>UI Not Built</title>
</head>
<body>
  <p>The UI is not built. Run npm run build in frontend/, or npm run dev for development.</p>
</body>
</html>
"""


def mount_ui(app: FastAPI, web_dir: Path) -> None:
    """Mounts static file serving and deep linking for the built UI as the router's fallback."""
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")

    resolved_web_dir = web_dir.resolve()

    async def ui_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose()(scope, receive, send)
            return

        if scope["type"] != "http":
            return

        method: str = scope["method"]
        if method not in ("GET", "HEAD"):
            raise HTTPException(status_code=404)

        raw_path: str = scope.get("path", "")
        if raw_path == "/api" or raw_path.startswith("/api/"):
            raise HTTPException(status_code=404)

        rel = raw_path.lstrip("/")
        try:
            candidate = (resolved_web_dir / rel).resolve()
        except (OSError, ValueError):
            raise HTTPException(status_code=404) from None

        if not candidate.is_relative_to(resolved_web_dir):
            raise HTTPException(status_code=404)

        response: Response
        if candidate.is_file():
            response = FileResponse(candidate)
        else:
            index_file = resolved_web_dir / "index.html"
            if index_file.is_file():
                response = FileResponse(index_file)
            else:
                response = HTMLResponse(content=NOT_BUILT_HTML)

        await response(scope, receive, send)

    app.router.default = ui_app
