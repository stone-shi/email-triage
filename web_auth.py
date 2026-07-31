"""Dashboard auth: identity resolution, cookie helpers, and Starlette route
decorators (this app has no FastAPI ``Depends``, so these are the equivalent).

Two separate identity mechanisms coexist on purpose:
- Browser/dashboard requests: an httpOnly session cookie (see users_store).
- MCP protocol requests (/sse, /messages/): a per-user bearer token (see
  mcp_tokens_store), resolved by AppAuthMiddleware in mcp_server.py, which sets
  the existing `current_profile` contextvar that get_resources() already reads
  -- so DB-backed tokens plug into the existing per-request profile-selection
  mechanism with no changes to get_resources() in this phase.
"""

from __future__ import annotations

import functools
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import appdb
import users_store

SESSION_COOKIE_NAME = "email_triage_session"


@dataclass
class CurrentIdentity:
    user_id: int
    username: str
    display_name: Optional[str]
    is_admin: bool
    is_active: bool
    must_change_password: bool
    kind: str  # 'session' | 'bearer'
    session_id: Optional[str] = None


def identity_from_user_row(row: sqlite3.Row, *, kind: str, session_id: Optional[str] = None) -> CurrentIdentity:
    return CurrentIdentity(
        user_id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        is_active=bool(row["is_active"]),
        must_change_password=bool(row["must_change_password"]),
        kind=kind,
        session_id=session_id,
    )


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def set_session_cookie(response: Response, token: str, *, max_age_seconds: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def _bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def resolve_identity_from_request(conn: sqlite3.Connection, request: Request) -> Optional[CurrentIdentity]:
    """Session cookie first (the normal browser path), then a bearer session
    token (for non-browser API callers that can't hold a cookie jar)."""
    token = request.cookies.get(SESSION_COOKIE_NAME) or _bearer_token(request)
    if not token:
        return None
    resolved = users_store.resolve_session(conn, token)
    if resolved is None:
        return None
    session, user = resolved
    users_store.touch_session(conn, session)
    return identity_from_user_row(user, kind="session", session_id=session["id"])


def _open_conn() -> sqlite3.Connection:
    conn = appdb.connect()
    return conn


def requires_user(fn: Callable) -> Callable:
    """401 unless some identity is present -- deliberately used (instead of
    requires_active_user) only by the handful of routes a password-change-
    forced user must still be able to reach: /api/auth/{me,change-password,logout}."""

    @functools.wraps(fn)
    async def wrapper(request: Request) -> Response:
        conn = _open_conn()
        try:
            identity = resolve_identity_from_request(conn, request)
            if identity is None:
                return error_response(401, "auth_required", "Login required")
            request.state.identity = identity
            request.state.conn = conn
            result = await fn(request)
            conn.commit()
            return result
        finally:
            conn.close()

    return wrapper


def requires_active_user(fn: Callable) -> Callable:
    """requires_user, plus a 409 password_change_required while the user's
    must_change_password flag is set -- every data route except the three
    above uses this one."""

    @functools.wraps(fn)
    async def wrapper(request: Request) -> Response:
        conn = _open_conn()
        try:
            identity = resolve_identity_from_request(conn, request)
            if identity is None:
                return error_response(401, "auth_required", "Login required")
            if identity.must_change_password:
                return error_response(
                    409, "password_change_required", "You must change your password before continuing"
                )
            request.state.identity = identity
            request.state.conn = conn
            result = await fn(request)
            conn.commit()
            return result
        finally:
            conn.close()

    return wrapper


def requires_admin(fn: Callable) -> Callable:
    """requires_active_user, plus a 403 unless the user is an admin."""

    @functools.wraps(fn)
    async def wrapper(request: Request) -> Response:
        conn = _open_conn()
        try:
            identity = resolve_identity_from_request(conn, request)
            if identity is None:
                return error_response(401, "auth_required", "Login required")
            if identity.must_change_password:
                return error_response(
                    409, "password_change_required", "You must change your password before continuing"
                )
            if not identity.is_admin:
                return error_response(403, "forbidden", "Administrator access required")
            request.state.identity = identity
            request.state.conn = conn
            result = await fn(request)
            conn.commit()
            return result
        finally:
            conn.close()

    return wrapper
