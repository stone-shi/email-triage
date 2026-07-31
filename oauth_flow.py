"""Generic OAuth2 authorization-code flow plumbing shared by Gmail and Zoho.

Provider-specific bits (scopes, endpoints, identity lookup, refresh quirks)
live in oauth_google.py/oauth_zoho.py; this module owns the parts that don't
vary: building an authorize URL, exchanging a code for tokens, refreshing, and
a signed, stateless `state` parameter (HMAC, no DB row needed) that survives
the round trip to the provider and back -- the OAuth callback route arrives
with no session cookie context guaranteed (the browser navigated in fresh
from Google/Zoho), so identity has to be proven by the state itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

STATE_TTL_SECONDS = 600


class OAuthError(Exception):
    """A provider call failed or a state/nonce couldn't be validated."""


def _signing_key() -> bytes:
    import os

    import secretstore

    env_key = os.getenv("EMAIL_TRIAGE_SECRET_KEY")
    if env_key:
        return env_key.encode()
    # Reuse the Fernet key file as the HMAC signing key -- one secret to
    # manage, and it's already generated + permission-checked by secretstore.
    _, key = secretstore.load_key()
    return key


def make_state(*, user_id: int, provider: str, nonce: str) -> str:
    """Signed, stateless OAuth state embedding who started the flow, for
    which provider, with a short TTL -- the callback trusts this instead of a
    session cookie or a DB row."""
    payload = {
        "user_id": user_id,
        "provider": provider,
        "nonce": nonce,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def parse_state(state: str) -> Dict[str, Any]:
    try:
        body, sig = state.split(".", 1)
    except (ValueError, AttributeError):
        raise OAuthError("Malformed OAuth state")

    expected_sig = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        raise OAuthError("OAuth state signature mismatch")

    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception as exc:
        raise OAuthError("Malformed OAuth state payload") from exc

    if payload.get("exp", 0) < time.time():
        raise OAuthError("OAuth state expired -- please retry connecting the account")
    return payload


@dataclass
class OAuthClient:
    provider: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    redirect_uri: str
    scopes: str
    extra_authorize_params: Tuple[Tuple[str, str], ...] = ()

    def build_authorize_url(self, *, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "state": state,
        }
        params.update(dict(self.extra_authorize_params))
        return f"{self.authorize_url}?{urlencode(params)}"

    def exchange_code(self, code: str) -> Dict[str, Any]:
        resp = httpx.post(
            self.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15.0,
        )
        if resp.status_code >= 400:
            raise OAuthError(f"{self.provider} token exchange failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        resp = httpx.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15.0,
        )
        if resp.status_code >= 400:
            raise OAuthError(f"{self.provider} token refresh failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()
