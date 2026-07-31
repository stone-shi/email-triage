"""Serves the built Vite/React SPA (web/dist) from the same Starlette app as
the MCP protocol endpoints and the JSON API -- registered as the LAST custom
route in mcp_server.py so it can never shadow /sse, /messages/, or /api/*.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

DEFAULT_DIST_DIR = Path(__file__).parent.resolve() / "web" / "dist"


def dist_dir() -> Path:
    override = os.getenv("EMAIL_TRIAGE_WEB_DIST")
    return Path(override).resolve() if override else DEFAULT_DIST_DIR


def _resolve_safe(dist: Path, url_path: str) -> Optional[Path]:
    """Resolve url_path against dist, rejecting any path-traversal escape."""
    candidate = (dist / url_path.lstrip("/")).resolve()
    try:
        candidate.relative_to(dist.resolve())
    except ValueError:
        return None
    return candidate


def spa_response(url_path: str, *, dist: Optional[Path] = None) -> Response:
    dist = dist or dist_dir()
    index_html = dist / "index.html"

    if url_path.startswith("api/"):
        return JSONResponse({"error": {"code": "not_found", "message": "Unknown API route"}}, status_code=404)

    if not index_html.exists():
        return JSONResponse(
            {
                "error": {
                    "code": "spa_not_built",
                    "message": "web/dist/index.html is missing -- run `npm run build` in web/",
                }
            },
            status_code=503,
        )

    candidate = _resolve_safe(dist, url_path)
    if candidate is not None and candidate.is_file():
        headers = {}
        if url_path.startswith("assets/"):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return FileResponse(candidate, headers=headers)

    return FileResponse(index_html, headers={"Cache-Control": "no-store"})


def register_spa_route(mcp) -> None:
    @mcp.custom_route("/{full_path:path}", methods=["GET"])
    async def spa_catch_all(request: Request) -> Response:
        return spa_response(request.path_params.get("full_path", ""))
