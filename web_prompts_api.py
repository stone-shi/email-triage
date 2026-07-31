"""Admin-only CRUD for the live triage pipeline's LLM system prompts.

register_prompts_routes(mcp) attaches these to the given FastMCP instance,
alongside web_api.py's auth/user/settings routes and
web_integrations_api.py's integration routes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import prompts_store
from web_auth import CurrentIdentity, error_response, requires_admin
from web_api import _json_body

# Same global prompts.yml the CLI/EmailTriageEngine falls back to -- prompts
# are process-wide config (like LLM model choice), not per-user.
_PROMPTS_YAML_PATH = Path(__file__).parent.resolve() / "prompts.yml"


def register_prompts_routes(mcp) -> None:
    @mcp.custom_route("/api/prompts", methods=["GET"])
    @requires_admin
    async def list_prompts(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        return JSONResponse({"prompts": prompts_store.get_all_prompts(conn, yaml_path=_PROMPTS_YAML_PATH)})

    @mcp.custom_route("/api/prompts/{key}", methods=["PUT"])
    @requires_admin
    async def update_prompt(request: Request) -> Response:
        key = request.path_params["key"]
        if key not in prompts_store.DEFAULT_PROMPTS:
            return error_response(404, "not_found", f"Unknown prompt key {key!r}")

        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        value = body.get("value")
        if not isinstance(value, str) or not value.strip():
            return error_response(400, "validation_error", "value must be a non-empty string")

        prompts_store.set_prompt(conn, key, value, updated_by=identity.user_id)
        return JSONResponse(prompts_store.get_all_prompts(conn, yaml_path=_PROMPTS_YAML_PATH)[key])

    @mcp.custom_route("/api/prompts/{key}/reset", methods=["POST"])
    @requires_admin
    async def reset_prompt_route(request: Request) -> Response:
        key = request.path_params["key"]
        if key not in prompts_store.DEFAULT_PROMPTS:
            return error_response(404, "not_found", f"Unknown prompt key {key!r}")

        conn: sqlite3.Connection = request.state.conn
        prompts_store.reset_prompt(conn, key)
        return JSONResponse(prompts_store.get_all_prompts(conn, yaml_path=_PROMPTS_YAML_PATH)[key])
