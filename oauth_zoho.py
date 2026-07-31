"""Zoho Mail OAuth2 -- a real OAuth connect option alongside (not replacing)
plain Zoho-over-IMAP-with-an-app-password, which keeps working unchanged as
its own `imap` provider integration.

Delivered as XOAUTH2 over the existing IMAPClient (see mail_auth.py), not a
separate Zoho Mail REST client: the pipeline's cache is keyed on the RFC
Message-ID, which Zoho's REST search API doesn't expose, so IMAP is the only
surface that doesn't require a schema change to the 600MB+ per-user cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

import app_settings_store
import oauth_flow
from token_service import ReauthRequired, TokenRefreshError

# IMAP/SMTP write access (marking \Seen, APPEND-ing drafts/sent copies) needs
# the ALL scope, not just READ.
SCOPES = "AaaServer.profile.READ ZohoMail.accounts.READ ZohoMail.messages.ALL"
DEFAULT_DC = "com"
_KNOWN_DCS = ("com", "eu", "in", "com.cn", "jp", "com.au")


def accounts_host(dc: str) -> str:
    return f"https://accounts.zoho.{dc}"


def dc_from_accounts_server(url: Optional[str]) -> Optional[str]:
    """Zoho's OAuth callback includes an `accounts-server` param naming the
    exact regional host that minted the grant -- trust that over any
    configured default, since a token is only valid in the DC that issued it."""
    if not url:
        return None
    stripped = url.rstrip("/")
    for dc in _KNOWN_DCS:
        if stripped.endswith(f"accounts.zoho.{dc}"):
            return dc
    return None


def resolve_dc(conn=None) -> str:
    if conn is not None:
        configured = app_settings_store.get_value(conn, "zoho_dc")
        if configured:
            return configured
    return DEFAULT_DC


def client_for(conn, *, redirect_uri: str, dc: Optional[str] = None) -> oauth_flow.OAuthClient:
    client_id = app_settings_store.get_value(conn, "zoho_client_id")
    client_secret = app_settings_store.get_value(conn, "zoho_client_secret")
    if not client_id or not client_secret:
        raise oauth_flow.OAuthError(
            "Zoho OAuth is not configured -- set zoho_client_id/zoho_client_secret in "
            "Admin → System Settings first."
        )
    resolved_dc = dc or resolve_dc(conn)
    base = accounts_host(resolved_dc)
    return oauth_flow.OAuthClient(
        provider="zoho",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=f"{base}/oauth/v2/auth",
        token_url=f"{base}/oauth/v2/token",
        redirect_uri=redirect_uri,
        scopes=SCOPES,
        # Without access_type=offline Zoho issues no refresh token at all.
        extra_authorize_params=(("access_type", "offline"), ("prompt", "consent")),
    )


def fetch_identity(access_token: str, *, dc: str = DEFAULT_DC) -> Dict[str, Any]:
    resp = httpx.get(
        f"{accounts_host(dc)}/oauth/user/info",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},  # not "Bearer" -- Zoho-specific
        timeout=15.0,
    )
    if resp.status_code >= 400:
        raise oauth_flow.OAuthError(f"Failed to fetch Zoho identity: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    email = (data.get("Email") or "").strip().lower()
    if not email:
        raise oauth_flow.OAuthError("Zoho identity response had no Email")
    return {"account_key": email, "email": email, "zuid": data.get("ZUID"), "dc": dc}


def refresh(secret: Dict[str, Any]) -> Dict[str, Any]:
    """Called by token_service.access_token(). Zoho access tokens live ~1hr;
    refresh tokens are long-lived and NOT rotated on refresh (no new
    refresh_token in the response), so the existing one is kept unless the
    provider explicitly issues a new one."""
    refresh_token = secret.get("refresh_token")
    if not refresh_token:
        raise ReauthRequired("No refresh token stored for this Zoho integration")
    client_id = secret.get("client_id")
    client_secret = secret.get("client_secret")
    dc = secret.get("dc", DEFAULT_DC)
    if not client_id or not client_secret:
        raise ReauthRequired("Zoho integration is missing its OAuth client credentials")

    base = accounts_host(dc)
    client = oauth_flow.OAuthClient(
        provider="zoho",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=f"{base}/oauth/v2/auth",
        token_url=f"{base}/oauth/v2/token",
        redirect_uri="",
        scopes=SCOPES,
    )
    try:
        granted = client.refresh(refresh_token)
    except oauth_flow.OAuthError as exc:
        # Zoho evicts the oldest refresh token past its per-client cap (~20);
        # an evicted or revoked token surfaces as an invalid/expired error.
        if "invalid" in str(exc).lower() or "expired" in str(exc).lower():
            raise ReauthRequired(f"Zoho refresh token was rejected: {exc}") from exc
        raise TokenRefreshError(str(exc)) from exc

    expires_in = granted.get("expires_in", 3600)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    result = {"access_token": granted["access_token"], "expires_at": expires_at}
    if granted.get("refresh_token"):
        result["refresh_token"] = granted["refresh_token"]
    return result
