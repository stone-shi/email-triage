"""Pluggable Gmail credential sources.

GmailClient historically read/wrote token.json against a fixed per-profile
path directly inside _authenticate(). FileTokenSource wraps that exact
behavior unchanged byte-for-byte (the default when no credential_source is
given, so the CLI/auto-rater scripts and the --auth headless flow need no
changes). DbTokenSource loads/saves an integrations row's encrypted
secret_json instead, for the new multi-account DB-backed flow -- note it
keeps client_id/client_secret PER INTEGRATION rather than reading a global
setting, because a refresh token is bound to the OAuth client that minted it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional, Protocol

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class ReauthRequired(Exception):
    """Raised by a CredentialSource that cannot interactively re-authenticate
    (e.g. DbTokenSource, running inside a web server request with no TTY/
    browser to complete an OAuth dance)."""


class CredentialSource(Protocol):
    def load(self) -> Optional[Credentials]: ...
    def save(self, creds: Credentials) -> None: ...
    def interactive_or_fail(self) -> Credentials: ...


class FileTokenSource:
    """The original per-profile token.json + client-secret-file flow."""

    def __init__(self, token_path: Path, credentials_path: Path, headless_mode: bool = False):
        self.token_path = Path(token_path)
        self.credentials_path = Path(credentials_path)
        self.headless_mode = headless_mode

    def load(self) -> Optional[Credentials]:
        if self.token_path.exists():
            return Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        return None

    def save(self, creds: Credentials) -> None:
        with open(self.token_path, "w") as token_file:
            token_file.write(creds.to_json())

    def interactive_or_fail(self) -> Credentials:
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Google client secrets file not found at {self.credentials_path}. "
                f"Please make sure gog credentials exist."
            )
        with open(self.credentials_path, "r") as f:
            raw_secrets = json.load(f)

        if "installed" in raw_secrets or "web" in raw_secrets:
            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
        else:
            client_config = {
                "installed": {
                    "client_id": raw_secrets.get("client_id"),
                    "client_secret": raw_secrets.get("client_secret"),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

        if self.headless_mode:
            flow.redirect_uri = "http://localhost"
            auth_url, _ = flow.authorization_url(access_type="offline", prompt="select_account")

            sys.stderr.write(
                f"\n[HEADLESS GMAIL OAUTH REQUIRED]:\n1. Open this URL in your desktop browser:\n"
                f"\U0001f449 {auth_url}\n\n"
            )
            sys.stderr.write(
                "2. Grant permissions and copy the FULL generated landing URL from your browser's "
                "address bar (starts with http://localhost...).\n"
            )
            sys.stderr.write("3. Paste the full redirect URL below:\n\n")
            sys.stderr.write("Paste FULL Redirect URL here: ")
            sys.stderr.flush()

            redirect_response = input()
            flow.fetch_token(authorization_response=redirect_response.strip())
            return flow.credentials

        def stderr_prompt_handler(url: str) -> None:
            sys.stderr.write(f"\n[GMAIL OAUTH REQUIRED]: Please visit this URL to authorize access:\n\U0001f449 {url}\n\n")
            sys.stderr.flush()

        return flow.run_local_server(port=0, prompt="select_account", authorization_prompt_handler=stderr_prompt_handler)


class DbTokenSource:
    """Loads/saves Gmail OAuth credentials from a data/app.db integrations row."""

    def __init__(self, integration_id: int):
        self.integration_id = integration_id

    def load(self) -> Optional[Credentials]:
        import appdb
        import integrations_store

        with appdb.get_conn() as conn:
            row = integrations_store.get_integration(conn, self.integration_id)
            if row is None or not row["secret_json"]:
                return None
            secret = integrations_store.get_secret(row)

        if not secret.get("refresh_token") and not secret.get("access_token"):
            return None
        return Credentials(
            token=secret.get("access_token"),
            refresh_token=secret.get("refresh_token"),
            token_uri=secret.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=secret.get("client_id"),
            client_secret=secret.get("client_secret"),
            scopes=SCOPES,
        )

    def save(self, creds: Credentials) -> None:
        import appdb
        import integrations_store

        with appdb.get_conn() as conn:
            row = integrations_store.get_integration(conn, self.integration_id)
            existing_secret = integrations_store.get_secret(row) if row is not None else {}
            secret = {
                **existing_secret,
                "access_token": creds.token,
                "refresh_token": creds.refresh_token or existing_secret.get("refresh_token"),
                "token_uri": creds.token_uri,
                "client_id": creds.client_id or existing_secret.get("client_id"),
                "client_secret": creds.client_secret or existing_secret.get("client_secret"),
            }
            expires_at = creds.expiry.isoformat() if getattr(creds, "expiry", None) else None
            integrations_store.update_integration(conn, self.integration_id, secret=secret)
            if expires_at:
                conn.execute(
                    "UPDATE integrations SET token_expires_at = ? WHERE id = ?",
                    (expires_at, self.integration_id),
                )

    def interactive_or_fail(self) -> Credentials:
        raise ReauthRequired(
            f"Gmail integration {self.integration_id} needs to be reconnected -- a server "
            "process cannot complete an interactive OAuth flow."
        )
