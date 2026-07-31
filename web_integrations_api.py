"""Per-user mailbox integration CRUD + OAuth connect flow (Gmail, Zoho, IMAP).

register_integrations_routes(mcp) attaches these to the given FastMCP
instance, alongside web_api.py's auth/user/settings routes.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

import account_clients
import app_settings_store
import appdb
import integrations_store as ints
import oauth_flow
import oauth_google
import oauth_zoho
from app_errors import AppStoreError, NotFoundError
from web_api import _cookie_secure, _json_body, _status_for
from web_auth import CurrentIdentity, error_response, requires_active_user

_OAUTH_PROVIDERS = ("gmail", "zoho")


def _oauth_settings_keys(provider: str) -> Dict[str, str]:
    prefix = "google" if provider == "gmail" else provider
    return {"client_id": f"{prefix}_client_id", "client_secret": f"{prefix}_client_secret"}


def _redirect_uri(base_url: str, provider: str) -> str:
    return f"{base_url.rstrip('/')}/api/integrations/oauth/{provider}/callback"


def register_integrations_routes(mcp) -> None:
    # ------------------------------------------------------------------ #
    # Providers / CRUD
    # ------------------------------------------------------------------ #

    @mcp.custom_route("/api/integrations/providers", methods=["GET"])
    @requires_active_user
    async def list_providers(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        base_url = app_settings_store.get_value(conn, "public_base_url") or ""
        base_ok = base_url.startswith("https://") or base_url.startswith("http://localhost")

        providers = []
        for pid, label in (("gmail", "Gmail"), ("zoho", "Zoho Mail")):
            keys = _oauth_settings_keys(pid)
            configured = bool(app_settings_store.get_value(conn, keys["client_id"])) and bool(
                app_settings_store.get_value(conn, keys["client_secret"])
            )
            if not base_ok:
                reason = (
                    "Set a https:// (or http://localhost) public base URL in "
                    "Admin → System Settings before connecting."
                )
                providers.append({"id": pid, "label": label, "auth_type": "oauth", "available": False, "unavailable_reason": reason})
            elif not configured:
                reason = f"Set {keys['client_id']}/{keys['client_secret']} in Admin → System Settings before connecting."
                providers.append({"id": pid, "label": label, "auth_type": "oauth", "available": False, "unavailable_reason": reason})
            else:
                providers.append({"id": pid, "label": label, "auth_type": "oauth", "available": True, "unavailable_reason": None})
        providers.append({"id": "imap", "label": "IMAP", "auth_type": "password", "available": True, "unavailable_reason": None})
        return JSONResponse({"providers": providers})

    @mcp.custom_route("/api/integrations", methods=["GET"])
    @requires_active_user
    async def list_integrations_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        target_user_id = identity.user_id
        requested = request.query_params.get("user_id")
        if requested and identity.is_admin:
            try:
                target_user_id = int(requested)
            except ValueError:
                return error_response(400, "validation_error", "user_id must be an integer")
        rows = ints.list_integrations(conn, target_user_id)
        return JSONResponse({"integrations": [ints.row_to_dict(r) for r in rows]})

    @mcp.custom_route("/api/integrations", methods=["POST"])
    @requires_active_user
    async def create_integration_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        body = await _json_body(request)
        if body.get("provider") != "imap":
            return error_response(
                400, "validation_error",
                "Only provider='imap' (password-based) can be created directly here -- "
                "connect gmail/zoho via GET /api/integrations/oauth/{provider}/start",
            )
        account_key = str(body.get("account_key", "")).strip()
        config = body.get("config") or {}
        secret = body.get("secret") or {}
        if not account_key or not config.get("host") or not secret.get("password"):
            return error_response(400, "validation_error", "account_key, config.host and secret.password are required")
        try:
            row = ints.create_integration(
                conn, user_id=identity.user_id, provider="imap", account_key=account_key,
                auth_type="password", account_label=body.get("account_label"), config=config, secret=secret,
            )
        except AppStoreError as exc:
            return error_response(_status_for(exc), exc.code, str(exc))
        return JSONResponse(ints.row_to_dict(row), status_code=201)

    @mcp.custom_route("/api/integrations/{integration_id}", methods=["PATCH"])
    @requires_active_user
    async def update_integration_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        try:
            integration_id = int(request.path_params["integration_id"])
        except ValueError:
            return error_response(400, "validation_error", "Invalid integration id")
        try:
            ints.require_own(conn, integration_id, identity.user_id)
        except NotFoundError as exc:
            return error_response(404, "not_found", str(exc))

        body = await _json_body(request)
        row = ints.update_integration(
            conn, integration_id,
            account_label=body.get("account_label"),
            enabled=body.get("enabled"),
            triage_enabled=body.get("triage_enabled"),
            archive_enabled=body.get("archive_enabled"),
            config=body.get("config"),
            secret=body.get("secret"),
        )
        return JSONResponse(ints.row_to_dict(row))

    @mcp.custom_route("/api/integrations/{integration_id}", methods=["DELETE"])
    @requires_active_user
    async def delete_integration_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        try:
            integration_id = int(request.path_params["integration_id"])
        except ValueError:
            return error_response(400, "validation_error", "Invalid integration id")
        try:
            ints.require_own(conn, integration_id, identity.user_id)
        except NotFoundError as exc:
            return error_response(404, "not_found", str(exc))
        ints.delete_integration(conn, integration_id)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/integrations/{integration_id}/test", methods=["POST"])
    @requires_active_user
    async def test_integration_route(request: Request) -> Response:
        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        try:
            integration_id = int(request.path_params["integration_id"])
        except ValueError:
            return error_response(400, "validation_error", "Invalid integration id")
        try:
            row = ints.require_own(conn, integration_id, identity.user_id)
        except NotFoundError as exc:
            return error_response(404, "not_found", str(exc))

        try:
            ac = account_clients.build_client_for_integration(row)
            result = account_clients.test_connection(ac)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        ints.record_test(conn, integration_id, ok=result["ok"], error=result.get("error"))
        return JSONResponse(result)

    # ------------------------------------------------------------------ #
    # OAuth connect flow
    # ------------------------------------------------------------------ #

    @mcp.custom_route("/api/integrations/oauth/{provider}/start", methods=["GET"])
    @requires_active_user
    async def oauth_start(request: Request) -> Response:
        import secrets as _secrets

        identity: CurrentIdentity = request.state.identity
        conn: sqlite3.Connection = request.state.conn
        provider = request.path_params["provider"]
        if provider not in _OAUTH_PROVIDERS:
            return error_response(400, "validation_error", f"Unknown OAuth provider {provider!r}")

        base_url = app_settings_store.get_value(conn, "public_base_url")
        if not base_url:
            return error_response(
                400, "validation_error",
                "Set a public base URL in Admin → System Settings before connecting an OAuth account.",
            )
        redirect_uri = _redirect_uri(base_url, provider)

        try:
            client = (oauth_google if provider == "gmail" else oauth_zoho).client_for(conn, redirect_uri=redirect_uri)
        except oauth_flow.OAuthError as exc:
            return error_response(400, "validation_error", str(exc))

        nonce = _secrets.token_urlsafe(16)
        state = oauth_flow.make_state(user_id=identity.user_id, provider=provider, nonce=nonce)
        response = JSONResponse({"authorize_url": client.build_authorize_url(state=state)})
        response.set_cookie(
            f"oauth_nonce_{provider}", nonce, max_age=oauth_flow.STATE_TTL_SECONDS,
            httponly=True, samesite="lax", secure=_cookie_secure(), path="/",
        )
        return response

    @mcp.custom_route("/api/integrations/oauth/{provider}/callback", methods=["GET"])
    async def oauth_callback(request: Request) -> Response:
        # Deliberately unauthenticated: the browser arrives here fresh from the
        # provider with no session cookie context guaranteed. The signed state
        # (proving who started the flow) plus the nonce cookie (CSRF defense)
        # stand in for a session.
        provider = request.path_params["provider"]
        spa_base = os.getenv("EMAIL_TRIAGE_SPA_REDIRECT_BASE", "")

        def redirect(query: str) -> Response:
            return RedirectResponse(url=f"{spa_base}/settings/integrations?{query}", status_code=303)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return redirect("error=" + quote("Missing code or state from provider"))

        try:
            payload = oauth_flow.parse_state(state)
        except oauth_flow.OAuthError as exc:
            return redirect("error=" + quote(str(exc)))

        if payload.get("provider") != provider:
            return redirect("error=" + quote("Provider mismatch in OAuth state"))

        nonce_cookie = request.cookies.get(f"oauth_nonce_{provider}")
        if not nonce_cookie or nonce_cookie != payload.get("nonce"):
            return redirect("error=" + quote("OAuth session expired or mismatched -- please retry"))

        conn = appdb.connect()
        try:
            try:
                base_url = app_settings_store.get_value(conn, "public_base_url") or ""
                redirect_uri = _redirect_uri(base_url, provider)
                if provider == "gmail":
                    _connect_gmail(conn, payload["user_id"], code, redirect_uri)
                else:
                    _connect_zoho(conn, payload["user_id"], code, redirect_uri, request)
                conn.commit()
            except oauth_flow.OAuthError as exc:
                return redirect("error=" + quote(str(exc)))
        finally:
            conn.close()

        response = redirect(f"connected={provider}")
        response.delete_cookie(f"oauth_nonce_{provider}", path="/")
        return response


def _connect_gmail(conn, user_id: int, code: str, redirect_uri: str) -> None:
    client = oauth_google.client_for(conn, redirect_uri=redirect_uri)
    granted = client.exchange_code(code)
    identity = oauth_google.fetch_identity(granted["access_token"])
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=granted.get("expires_in", 3600))).isoformat()
    ints.upsert_oauth_integration(
        conn,
        user_id=user_id,
        provider="gmail",
        account_key=identity["account_key"],
        secret={
            "access_token": granted.get("access_token"),
            "refresh_token": granted.get("refresh_token"),
            "token_uri": oauth_google.TOKEN_URL,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        },
        scopes=oauth_google.SCOPES,
        token_expires_at=expires_at,
    )


def _connect_zoho(conn, user_id: int, code: str, redirect_uri: str, request: Request) -> None:
    dc_hint = oauth_zoho.dc_from_accounts_server(request.query_params.get("accounts-server"))
    client = oauth_zoho.client_for(conn, redirect_uri=redirect_uri, dc=dc_hint)
    granted = client.exchange_code(code)
    dc = dc_hint or oauth_zoho.resolve_dc(conn)
    identity = oauth_zoho.fetch_identity(granted["access_token"], dc=dc)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=granted.get("expires_in", 3600))).isoformat()
    ints.upsert_oauth_integration(
        conn,
        user_id=user_id,
        provider="zoho",
        account_key=identity["account_key"],
        secret={
            "access_token": granted.get("access_token"),
            "refresh_token": granted.get("refresh_token"),
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "dc": dc,
        },
        scopes=oauth_zoho.SCOPES,
        token_expires_at=expires_at,
        config={
            "dc": dc,
            "imap_host": "imap.zoho.com",
            "imap_port": 993,
            "smtp_host": "smtp.zoho.com",
            "smtp_port": 465,
            "zuid": identity.get("zuid"),
        },
    )
