"""Gmail OAuth2 -- the browser-based "Connect Gmail" flow for the dashboard,
alongside (not replacing) the existing CLI headless/local-server flow in
gmail_client.py/credential_source.py, which is unaffected.

Client credentials are baked into each integration's encrypted secret at
connect time (see web_api.py's oauth callback) rather than looked up fresh
from app_settings on every refresh, because a refresh token is bound to the
OAuth client that minted it -- migrated legacy rows keep their original
per-profile Desktop-app client, while freshly-connected rows get a copy of
the shared app-level Web-app client. refresh() below only ever reads from the
secret dict for this reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx

import app_settings_store
import oauth_flow
from token_service import ReauthRequired, TokenRefreshError

SCOPES = "https://www.googleapis.com/auth/gmail.modify"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def client_for(conn, *, redirect_uri: str) -> oauth_flow.OAuthClient:
    client_id = app_settings_store.get_value(conn, "google_client_id")
    client_secret = app_settings_store.get_value(conn, "google_client_secret")
    if not client_id or not client_secret:
        raise oauth_flow.OAuthError(
            "Gmail OAuth is not configured -- set google_client_id/google_client_secret in "
            "Admin → System Settings first."
        )
    return oauth_flow.OAuthClient(
        provider="gmail",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        redirect_uri=redirect_uri,
        scopes=SCOPES,
        extra_authorize_params=(
            ("access_type", "offline"),
            ("prompt", "consent"),
            ("include_granted_scopes", "true"),
        ),
    )


def fetch_identity(access_token: str) -> Dict[str, Any]:
    resp = httpx.get(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15.0)
    if resp.status_code >= 400:
        raise oauth_flow.OAuthError(f"Failed to fetch Gmail identity: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    email = (data.get("emailAddress") or "").strip().lower()
    if not email:
        raise oauth_flow.OAuthError("Gmail identity response had no emailAddress")
    return {"account_key": email, "email": email}


def refresh(secret: Dict[str, Any]) -> Dict[str, Any]:
    """Called by token_service.access_token(). Returns fields to merge into
    the stored secret (`access_token`, `refresh_token` only if reissued) plus
    `expires_at` for token_expires_at."""
    refresh_token = secret.get("refresh_token")
    if not refresh_token:
        raise ReauthRequired("No refresh token stored for this Gmail integration")
    client_id = secret.get("client_id")
    client_secret = secret.get("client_secret")
    if not client_id or not client_secret:
        raise ReauthRequired("Gmail integration is missing its OAuth client credentials")

    client = oauth_flow.OAuthClient(
        provider="gmail",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        redirect_uri="",
        scopes=SCOPES,
    )
    try:
        granted = client.refresh(refresh_token)
    except oauth_flow.OAuthError as exc:
        if "invalid_grant" in str(exc).lower():
            raise ReauthRequired(f"Gmail refresh token was rejected: {exc}") from exc
        raise TokenRefreshError(str(exc)) from exc

    expires_in = granted.get("expires_in", 3600)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    result = {"access_token": granted["access_token"], "expires_at": expires_at}
    if granted.get("refresh_token"):
        result["refresh_token"] = granted["refresh_token"]
    return result
