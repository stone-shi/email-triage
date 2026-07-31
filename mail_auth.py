"""Pluggable IMAP/SMTP authentication strategies.

IMAPClient historically logged in with a flat login/password pair read
straight off Settings. PasswordMailAuth/PasswordSmtpAuth wrap that exact
behavior unchanged (the default when no auth strategy is given, so the CLI and
auto-rater scripts need no changes). XOAuth2MailAuth/XOAuth2SmtpAuth back a
connection with a live OAuth access token instead (via token_service),
letting Zoho-OAuth accounts reuse the same IMAPClient/imap_tools machinery
as plain password-based IMAP -- see CLAUDE.md's "Real Zoho OAuth2" note on why
this is XOAUTH2-over-IMAP rather than a separate REST client (RFC Message-ID
as the cache's primary key isn't exposed by Zoho's REST search API).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol


class MailAuth(Protocol):
    def attach(self, box: Any, username: str) -> Any:
        """Authenticate an (unconnected) imap_tools.MailBox and return it,
        ready to use as a context manager -- mirrors the shape of both
        MailBox.login() and MailBox.xoauth2(), which each return self."""
        ...


class PasswordMailAuth:
    def __init__(self, password: str):
        self.password = password

    def attach(self, box: Any, username: str) -> Any:
        return box.login(username, self.password)


class XOAuth2MailAuth:
    def __init__(self, integration_id: int):
        self.integration_id = integration_id

    def attach(self, box: Any, username: str) -> Any:
        import token_service

        access_token = token_service.access_token(self.integration_id)
        return box.xoauth2(username, access_token)


class SmtpAuth(Protocol):
    def authenticate(self, server: Any, username: str) -> None: ...


class PasswordSmtpAuth:
    def __init__(self, password: Optional[str]):
        self.password = password

    def authenticate(self, server: Any, username: str) -> None:
        server.login(username, self.password)


def _xoauth2_sasl_string(username: str, access_token: str) -> str:
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01"


class XOAuth2SmtpAuth:
    def __init__(self, integration_id: int):
        self.integration_id = integration_id

    def authenticate(self, server: Any, username: str) -> None:
        import token_service

        access_token = token_service.access_token(self.integration_id)
        sasl_string = _xoauth2_sasl_string(username, access_token)
        # smtplib base64-encodes whatever the authobject callable returns, so
        # this must return the raw SASL string, not a pre-encoded one.
        server.auth("XOAUTH2", lambda challenge=None: sasl_string, initial_response_ok=True)
