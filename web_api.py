"""Auth / user-management / global-settings dashboard API routes.

register_web_routes(mcp) attaches every route below to the given FastMCP
instance via mcp.custom_route(...). Kept separate from mcp_server.py (which
already carries the MCP tool definitions and the pre-existing dashboard status/
sync/logs routes) so this new surface is easy to find and reason about on its
own. The existing dashboard routes are intentionally left where they are and
unauthenticated for now -- they get folded into this auth model as part of the
SPA cutover, once there's a login page for them to redirect to.
"""

from __future__ import annotations

import secrets as _secrets
import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import app_settings_store
import appdb
import connection_check
import mcp_tokens_store
import users_store
from app_errors import AppStoreError, NotFoundError, ValidationError
from security import validate_password, verify_password
from web_auth import (
    CurrentIdentity,
    clear_session_cookie,
    error_response,
    requires_active_user,
    requires_admin,
    requires_user,
    set_session_cookie,
)

_STATUS_FOR_CODE = {
    "not_found": 404,
    "conflict": 409,
    "validation_error": 400,
    "forbidden": 403,
}


def _status_for(exc: AppStoreError) -> int:
    return _STATUS_FOR_CODE.get(exc.code, 400)


def _cookie_secure() -> bool:
    import os

    override = os.getenv("EMAIL_TRIAGE_SESSION_COOKIE_SECURE")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes")
    base_url = os.getenv("EMAIL_TRIAGE_PUBLIC_BASE_URL", "")
    return base_url.startswith("https://")


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def register_web_routes(mcp) -> None:
    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    @mcp.custom_route("/api/auth/login", methods=["POST"])
    async def auth_login(request: Request) -> Response:
        body = await _json_body(request)
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            return error_response(400, "validation_error", "username and password are required")

        conn = appdb.connect()
        try:
            user = users_store.authenticate(conn, username, password)
            if user is None:
                conn.commit()
                return error_response(401, "invalid_credentials", "Invalid username or password")
            token, _session_id = users_store.create_session(
                conn,
                user["id"],
                user_agent=request.headers.get("user-agent"),
                ip=request.client.host if request.client else None,
            )
            conn.commit()
        finally:
            conn.close()

        response = JSONResponse(
            {"user": users_store.row_to_user(user), "must_change_password": bool(user["must_change_password"])}
        )
        set_session_cookie(
            response, token, max_age_seconds=users_store.session_ttl_hours() * 3600, secure=_cookie_secure()
        )
        return response

    @mcp.custom_route("/api/auth/logout", methods=["POST"])
    @requires_user
    async def auth_logout(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        if identity.session_id:
            users_store.delete_session(conn, identity.session_id)
        response = JSONResponse({"ok": True})
        clear_session_cookie(response)
        return response

    @mcp.custom_route("/api/auth/me", methods=["GET"])
    @requires_user
    async def auth_me(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        return JSONResponse(users_store.row_to_user(users_store.get_user(conn, identity.user_id)))

    @mcp.custom_route("/api/auth/change-password", methods=["POST"])
    @requires_user
    async def auth_change_password(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        current_password = str(body.get("current_password", ""))
        new_password = str(body.get("new_password", ""))

        row = users_store.get_user(conn, identity.user_id)
        ok = verify_password(
            current_password,
            password_hash=row["password_hash"],
            password_salt=row["password_salt"],
            password_algo=row["password_algo"],
            password_params=row["password_params"],
        )
        if not ok:
            return error_response(400, "validation_error", "Current password is incorrect")

        error = validate_password(
            new_password, min_length=users_store.password_min_length(), current=current_password
        )
        if error:
            return error_response(400, "validation_error", error)

        users_store.set_password(conn, identity.user_id, new_password, must_change=False)
        revoked = users_store.delete_other_sessions(conn, identity.user_id, keep_session_id=identity.session_id)
        return JSONResponse({"ok": True, "revoked_sessions": revoked})

    # ------------------------------------------------------------------ #
    # Self-service MCP tokens
    # ------------------------------------------------------------------ #

    @mcp.custom_route("/api/me/mcp-tokens", methods=["GET"])
    @requires_active_user
    async def list_mcp_tokens(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        rows = mcp_tokens_store.list_tokens(conn, identity.user_id)
        return JSONResponse({"tokens": [mcp_tokens_store.row_to_dict(r) for r in rows]})

    @mcp.custom_route("/api/me/mcp-tokens", methods=["POST"])
    @requires_active_user
    async def create_mcp_token_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        raw, row = mcp_tokens_store.create_token(conn, identity.user_id, label=body.get("label"))
        return JSONResponse({"token": raw, **mcp_tokens_store.row_to_dict(row)}, status_code=201)

    @mcp.custom_route("/api/me/mcp-tokens/{token_id}", methods=["DELETE"])
    @requires_active_user
    async def revoke_mcp_token_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        try:
            token_id = int(request.path_params["token_id"])
        except (KeyError, ValueError):
            return error_response(400, "validation_error", "Invalid token id")
        try:
            mcp_tokens_store.revoke_token(conn, identity.user_id, token_id)
        except NotFoundError as exc:
            return error_response(404, "not_found", str(exc))
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ #
    # Admin: users
    # ------------------------------------------------------------------ #

    @mcp.custom_route("/api/users", methods=["GET"])
    @requires_admin
    async def list_users_route(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        include_inactive = request.query_params.get("include_inactive") == "1"
        try:
            page = int(request.query_params.get("page", 1))
            page_size = min(int(request.query_params.get("page_size", 50)), 200)
        except ValueError:
            return error_response(400, "validation_error", "page/page_size must be integers")
        rows, total = users_store.list_users(
            conn, include_inactive=include_inactive, page=page, page_size=page_size
        )
        return JSONResponse(
            {"items": [users_store.row_to_user(r) for r in rows], "page": page, "page_size": page_size, "total": total}
        )

    @mcp.custom_route("/api/users", methods=["POST"])
    @requires_admin
    async def create_user_route(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        try:
            row = users_store.create_user(
                conn,
                username=str(body.get("username", "")).strip(),
                password=str(body.get("password", "")),
                display_name=body.get("display_name"),
                is_admin=bool(body.get("is_admin", False)),
            )
        except AppStoreError as exc:
            return error_response(_status_for(exc), exc.code, str(exc))
        return JSONResponse(users_store.row_to_user(row), status_code=201)

    @mcp.custom_route("/api/users/{user_id}", methods=["PATCH"])
    @requires_admin
    async def update_user_route(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        try:
            user_id = int(request.path_params["user_id"])
        except (KeyError, ValueError):
            return error_response(400, "validation_error", "Invalid user id")
        body = await _json_body(request)
        try:
            row = users_store.update_user(
                conn,
                user_id,
                display_name=body.get("display_name"),
                is_admin=body.get("is_admin"),
                is_active=body.get("is_active"),
            )
        except AppStoreError as exc:
            return error_response(_status_for(exc), exc.code, str(exc))
        return JSONResponse(users_store.row_to_user(row))

    @mcp.custom_route("/api/users/{user_id}/reset-password", methods=["POST"])
    @requires_admin
    async def reset_password_route(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        try:
            user_id = int(request.path_params["user_id"])
        except (KeyError, ValueError):
            return error_response(400, "validation_error", "Invalid user id")
        row = users_store.get_user(conn, user_id)
        if row is None:
            return error_response(404, "not_found", "User not found")
        body = await _json_body(request)
        new_password = body.get("new_password") or _secrets.token_urlsafe(9)
        users_store.set_password(conn, user_id, new_password, must_change=True)
        users_store.delete_other_sessions(conn, user_id, keep_session_id=None)
        return JSONResponse(
            {"user": users_store.row_to_user(users_store.get_user(conn, user_id)), "temporary_password": new_password}
        )

    @mcp.custom_route("/api/users/{user_id}", methods=["DELETE"])
    @requires_admin
    async def delete_user_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        try:
            user_id = int(request.path_params["user_id"])
        except (KeyError, ValueError):
            return error_response(400, "validation_error", "Invalid user id")
        try:
            users_store.delete_user(conn, user_id, requesting_user_id=identity.user_id)
        except AppStoreError as exc:
            return error_response(_status_for(exc), exc.code, str(exc))
        return JSONResponse({"ok": True, "deactivated": True})

    # ------------------------------------------------------------------ #
    # Global / per-user settings
    # ------------------------------------------------------------------ #

    @mcp.custom_route("/api/settings", methods=["GET"])
    @requires_active_user
    async def get_settings_route(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        return JSONResponse({"settings": app_settings_store.get_all_for_api(conn)})

    @mcp.custom_route("/api/settings", methods=["PUT"])
    @requires_admin
    async def put_settings_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        values = body.get("values") or {}
        try:
            updated = app_settings_store.set_many(conn, values, updated_by=identity.user_id)
        except ValidationError as exc:
            return error_response(400, "validation_error", str(exc))
        return JSONResponse({"ok": True, "updated": updated})

    @mcp.custom_route("/api/settings/test", methods=["POST"])
    @requires_admin
    async def test_settings_route(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        kind = body.get("kind")
        overrides = body.get("values") or {}

        def resolved(key: str):
            override = overrides.get(key)
            if override not in (None, ""):
                return override
            return app_settings_store.get_value(conn, key)

        if kind == "triage":
            result = connection_check.test_chat_completion(
                resolved("triage_base_url"), resolved("triage_api_key"), resolved("triage_model")
            )
        elif kind == "summary":
            result = connection_check.test_chat_completion(
                resolved("summary_base_url"), resolved("summary_api_key"), resolved("summary_model")
            )
        elif kind == "quality_judge":
            result = connection_check.test_chat_completion(
                resolved("quality_check.judge_base_url"),
                resolved("quality_check.judge_api_key"),
                resolved("quality_check.judge_model"),
            )
        elif kind == "tei":
            result = connection_check.test_rerank(
                resolved("tei_url"), resolved("tei_api_key"), resolved("tei_model")
            )
        else:
            return error_response(400, "validation_error", f"Unknown test kind {kind!r}")
        return JSONResponse(result)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})
