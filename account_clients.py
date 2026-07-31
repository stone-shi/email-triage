"""Builds live GmailClient/IMAPClient instances from a user's data/app.db
integrations rows -- one AccountClient per enabled row, replacing the
hardcoded "exactly one Gmail + one IMAP" shape.

Falls back to the legacy single-Gmail+single-IMAP construction (straight off
a Settings instance, exactly like today) when a user has zero integrations
rows, so accounts that haven't been migrated into the DB yet -- i.e. every
account, until migrate_to_db.py has run -- keep working unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

import credential_source
import integrations_store as ints
import mail_auth
from config import PLACEHOLDER_GMAIL_ACCOUNT, PLACEHOLDER_IMAP_LOGIN
from gmail_client import GmailClient
from imap_client import IMAPClient

logger = logging.getLogger("email_triage.account_clients")


@dataclass
class AccountClient:
    integration_id: Optional[int]
    provider: str
    account: str
    label: str
    client: Any
    triage_enabled: bool = True
    archive_enabled: bool = True


def _build_gmail(row) -> AccountClient:
    client = GmailClient(credential_source=credential_source.DbTokenSource(row["id"]))
    return AccountClient(
        integration_id=row["id"],
        provider="gmail",
        account=row["cache_account_key"],
        label=row["account_label"] or row["cache_account_key"],
        client=client,
        triage_enabled=bool(row["triage_enabled"]),
        archive_enabled=bool(row["archive_enabled"]),
    )


def _build_zoho(row) -> AccountClient:
    config = json.loads(row["config_json"] or "{}")
    client = IMAPClient(
        host=config.get("imap_host", "imap.zoho.com"),
        port=config.get("imap_port", 993),
        login=row["cache_account_key"],
        mail_auth=mail_auth.XOAuth2MailAuth(row["id"]),
        smtp_host=config.get("smtp_host", "smtp.zoho.com"),
        smtp_port=config.get("smtp_port", 465),
        smtp_login=row["cache_account_key"],
        smtp_auth=mail_auth.XOAuth2SmtpAuth(row["id"]),
    )
    return AccountClient(
        integration_id=row["id"],
        provider="zoho",
        account=row["cache_account_key"],
        label=row["account_label"] or row["cache_account_key"],
        client=client,
        triage_enabled=bool(row["triage_enabled"]),
        archive_enabled=bool(row["archive_enabled"]),
    )


def _build_imap(row) -> AccountClient:
    config = json.loads(row["config_json"] or "{}")
    secret = ints.get_secret(row)
    client = IMAPClient(
        host=config.get("host"),
        port=config.get("port"),
        login=row["cache_account_key"],
        mail_auth=mail_auth.PasswordMailAuth(secret.get("password", "")),
        smtp_host=config.get("smtp_host"),
        smtp_port=config.get("smtp_port"),
        smtp_login=config.get("smtp_login") or row["cache_account_key"],
        smtp_auth=mail_auth.PasswordSmtpAuth(secret.get("smtp_password") or secret.get("password", "")),
    )
    return AccountClient(
        integration_id=row["id"],
        provider="imap",
        account=row["cache_account_key"],
        label=row["account_label"] or row["cache_account_key"],
        client=client,
        triage_enabled=bool(row["triage_enabled"]),
        archive_enabled=bool(row["archive_enabled"]),
    )


def build_client_for_integration(row) -> AccountClient:
    if row["provider"] == "gmail":
        return _build_gmail(row)
    if row["provider"] == "zoho":
        return _build_zoho(row)
    if row["provider"] == "imap":
        return _build_imap(row)
    raise ValueError(f"Unknown integration provider {row['provider']!r}")


def test_connection(ac: AccountClient) -> dict:
    """Lightweight reachability/auth check for an already-built AccountClient
    -- a real login attempt (IMAP connect, or a cheap Gmail profile fetch),
    not just "does the client object exist"."""
    try:
        if ac.provider == "gmail":
            ac.client.service.users().getProfile(userId="me").execute()
        else:
            with ac.client._mailbox():
                pass
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _legacy_accounts(settings_instance) -> List[AccountClient]:
    """Pre-migration fallback: today's one-Gmail-plus-one-IMAP shape, built
    straight off a Settings instance. integration_id is None -- there is no
    DB row to key by."""
    accounts: List[AccountClient] = []

    gmail_account = getattr(settings_instance, "gmail_account", "") or ""
    if gmail_account and gmail_account != PLACEHOLDER_GMAIL_ACCOUNT:
        try:
            client = GmailClient(settings_instance=settings_instance)
            accounts.append(
                AccountClient(
                    integration_id=None, provider="gmail", account=gmail_account,
                    label=gmail_account, client=client,
                )
            )
        except Exception:
            logger.exception("Legacy Gmail client construction failed for %s", gmail_account)

    imap_login = getattr(settings_instance, "imap_login", "") or ""
    if imap_login and imap_login != PLACEHOLDER_IMAP_LOGIN and getattr(settings_instance, "imap_password", ""):
        try:
            client = IMAPClient(settings_instance=settings_instance)
            accounts.append(
                AccountClient(
                    integration_id=None, provider="imap", account=imap_login,
                    label=imap_login, client=client,
                )
            )
        except Exception:
            logger.exception("Legacy IMAP client construction failed for %s", imap_login)

    return accounts


def clients_for_user(
    conn, user_id: int, settings_instance, *, for_triage: bool = False, for_archive: bool = False
) -> List[AccountClient]:
    """One AccountClient per enabled integrations row for this user. A row
    that fails to build (revoked token, bad password) is skipped -- with the
    error recorded on the row -- rather than raised, so one dead mailbox can't
    stop the rest of a sync. Falls back to _legacy_accounts when the user has
    no integrations rows at all.
    """
    rows = ints.list_integrations(conn, user_id, enabled_only=True)
    if for_triage:
        rows = [r for r in rows if r["triage_enabled"]]
    if for_archive:
        rows = [r for r in rows if r["archive_enabled"]]

    if not rows:
        return _legacy_accounts(settings_instance)

    accounts: List[AccountClient] = []
    for row in rows:
        try:
            accounts.append(build_client_for_integration(row))
        except Exception as exc:
            logger.exception("Failed to build client for integration %s (%s)", row["id"], row["provider"])
            ints.record_test(conn, row["id"], ok=False, error=str(exc))
    return accounts
