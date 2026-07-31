"""Per-user mailbox integrations (data/app.db, table ``integrations``).

One row per connected mailbox. A user can connect any number of Gmail, Zoho,
or plain-IMAP accounts -- the UNIQUE index is on (user_id, provider,
account_key), not (user_id, provider), specifically so two Gmail accounts for
the same user coexist. Secrets (OAuth tokens, IMAP/SMTP passwords, per-
integration OAuth client id/secret) are stored as a single Fernet envelope in
``secret_json`` via secretstore.encrypt/decrypt -- never touched directly by
callers outside this module.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

import secretstore
from appdb import utcnow
from app_errors import ConflictError, NotFoundError, ValidationError

PROVIDERS = ("gmail", "zoho", "imap")
AUTH_TYPES = ("oauth", "password")


def _mask_secret_preview(secret: Dict[str, Any]) -> Optional[str]:
    """A short non-reversible hint ('unchanged' echo target for PATCH), never
    the real secret value."""
    for key in ("password", "refresh_token", "access_token"):
        val = secret.get(key)
        if val:
            return f"••••{str(val)[-4:]}"
    return None


def row_to_dict(row: sqlite3.Row, *, include_secret_preview: bool = True) -> dict:
    config = json.loads(row["config_json"] or "{}")
    d = {
        "id": row["id"],
        "user_id": row["user_id"],
        "provider": row["provider"],
        "account_key": row["account_key"],
        "account_label": row["account_label"],
        "cache_account_key": row["cache_account_key"],
        "enabled": bool(row["enabled"]),
        "triage_enabled": bool(row["triage_enabled"]),
        "archive_enabled": bool(row["archive_enabled"]),
        "auth_type": row["auth_type"],
        "config": config,
        "scopes": row["scopes"],
        "token_expires_at": row["token_expires_at"],
        "status": row["status"],
        "last_test_at": row["last_test_at"],
        "last_test_ok": bool(row["last_test_ok"]) if row["last_test_ok"] is not None else None,
        "last_test_error": row["last_test_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "has_secret": bool(row["secret_json"]),
    }
    if include_secret_preview and row["secret_json"]:
        try:
            d["secret_preview"] = _mask_secret_preview(secretstore.decrypt(row["secret_json"]))
        except secretstore.SecretDecryptError:
            d["secret_preview"] = None
    return d


def derive_cache_account_key(conn: sqlite3.Connection, user_id: int, account_key: str) -> str:
    """The immutable string written into email_cache.account for this mailbox.

    Usually just the lowercased address; disambiguated with '#<n>' only when the
    same user connects the same address through a second provider/row (e.g.
    Zoho-OAuth and legacy Zoho-password side by side during a transition).
    """
    base = account_key.strip().lower()
    existing = conn.execute(
        "SELECT 1 FROM integrations WHERE user_id = ? AND cache_account_key = ?", (user_id, base)
    ).fetchone()
    if not existing:
        return base
    n = 2
    while conn.execute(
        "SELECT 1 FROM integrations WHERE user_id = ? AND cache_account_key = ?", (user_id, f"{base}#{n}")
    ).fetchone():
        n += 1
    return f"{base}#{n}"


def list_integrations(
    conn: sqlite3.Connection, user_id: Optional[int] = None, *, enabled_only: bool = False
) -> List[sqlite3.Row]:
    where, params = [], []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if enabled_only:
        where.append("enabled = 1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(f"SELECT * FROM integrations {clause} ORDER BY created_at", params).fetchall()


def get_integration(conn: sqlite3.Connection, integration_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM integrations WHERE id = ?", (integration_id,)).fetchone()


def require_own(conn: sqlite3.Connection, integration_id: int, user_id: int) -> sqlite3.Row:
    """Someone else's integration is a 404, not a 403 -- a 403 would confirm it exists."""
    row = get_integration(conn, integration_id)
    if row is None or row["user_id"] != user_id:
        raise NotFoundError("Integration not found")
    return row


def get_secret(row: sqlite3.Row) -> dict:
    return secretstore.decrypt(row["secret_json"])


def create_integration(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    provider: str,
    account_key: str,
    auth_type: str,
    account_label: Optional[str] = None,
    config: Optional[dict] = None,
    secret: Optional[dict] = None,
    scopes: Optional[str] = None,
    status: str = "unverified",
    token_expires_at: Optional[str] = None,
    cache_account_key: Optional[str] = None,
) -> sqlite3.Row:
    if provider not in PROVIDERS:
        raise ValidationError(f"Unknown provider {provider!r}")
    if auth_type not in AUTH_TYPES:
        raise ValidationError(f"Unknown auth_type {auth_type!r}")

    account_key = account_key.strip().lower()
    existing = conn.execute(
        "SELECT * FROM integrations WHERE user_id = ? AND provider = ? AND account_key = ?",
        (user_id, provider, account_key),
    ).fetchone()
    if existing is not None:
        raise ConflictError(f"{provider} account {account_key!r} is already connected")

    cache_key = cache_account_key or derive_cache_account_key(conn, user_id, account_key)
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO integrations (user_id, provider, account_key, account_label, cache_account_key,
                                  auth_type, config_json, secret_json, scopes, status,
                                  token_expires_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            provider,
            account_key,
            account_label,
            cache_key,
            auth_type,
            json.dumps(config or {}),
            secretstore.encrypt(secret) if secret else None,
            scopes,
            status,
            token_expires_at,
            now,
            now,
        ),
    )
    return get_integration(conn, cur.lastrowid)


def upsert_oauth_integration(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    provider: str,
    account_key: str,
    secret: dict,
    account_label: Optional[str] = None,
    config: Optional[dict] = None,
    scopes: Optional[str] = None,
    token_expires_at: Optional[str] = None,
) -> sqlite3.Row:
    """Create-or-refresh-credentials for an OAuth account. Reconnecting the same
    address updates the existing row's secret rather than piling up duplicates
    -- important for providers (Zoho) that cap live refresh tokens per client
    and silently invalidate the oldest beyond the cap."""
    account_key = account_key.strip().lower()
    existing = conn.execute(
        "SELECT * FROM integrations WHERE user_id = ? AND provider = ? AND account_key = ?",
        (user_id, provider, account_key),
    ).fetchone()
    now = utcnow()
    if existing is None:
        return create_integration(
            conn,
            user_id=user_id,
            provider=provider,
            account_key=account_key,
            auth_type="oauth",
            account_label=account_label or account_key,
            config=config,
            secret=secret,
            scopes=scopes,
            status="ok",
            token_expires_at=token_expires_at,
        )
    conn.execute(
        """
        UPDATE integrations
           SET secret_json = ?, secret_version = secret_version + 1, config_json = ?,
               scopes = ?, token_expires_at = ?, status = 'ok', enabled = 1,
               last_test_error = NULL, updated_at = ?
         WHERE id = ?
        """,
        (
            secretstore.encrypt(secret),
            json.dumps(config or json.loads(existing["config_json"] or "{}")),
            scopes,
            token_expires_at,
            now,
            existing["id"],
        ),
    )
    return get_integration(conn, existing["id"])


def update_integration(
    conn: sqlite3.Connection,
    integration_id: int,
    *,
    account_label: Optional[str] = None,
    enabled: Optional[bool] = None,
    triage_enabled: Optional[bool] = None,
    archive_enabled: Optional[bool] = None,
    config: Optional[dict] = None,
    secret: Optional[dict] = None,
) -> sqlite3.Row:
    row = get_integration(conn, integration_id)
    if row is None:
        raise NotFoundError("Integration not found")

    fields, params = [], []
    if account_label is not None:
        fields.append("account_label = ?")
        params.append(account_label)
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(int(enabled))
    if triage_enabled is not None:
        fields.append("triage_enabled = ?")
        params.append(int(triage_enabled))
    if archive_enabled is not None:
        fields.append("archive_enabled = ?")
        params.append(int(archive_enabled))
    if config is not None:
        merged = json.loads(row["config_json"] or "{}")
        merged.update(config)
        fields.append("config_json = ?")
        params.append(json.dumps(merged))
    if secret is not None:
        fields.append("secret_json = ?")
        params.append(secretstore.encrypt(secret) if secret else None)
        fields.append("secret_version = secret_version + 1")
    if fields:
        fields.append("updated_at = ?")
        params.append(utcnow())
        params.append(integration_id)
        conn.execute(f"UPDATE integrations SET {', '.join(fields)} WHERE id = ?", params)
    return get_integration(conn, integration_id)


def delete_integration(conn: sqlite3.Connection, integration_id: int) -> None:
    """Deletes the credential row only -- cached mail in email_cache.db is untouched."""
    conn.execute("DELETE FROM integrations WHERE id = ?", (integration_id,))


def record_test(
    conn: sqlite3.Connection, integration_id: int, *, ok: bool, error: Optional[str] = None
) -> None:
    conn.execute(
        """
        UPDATE integrations
           SET last_test_at = ?, last_test_ok = ?, last_test_error = ?,
               status = ?, updated_at = ?
         WHERE id = ?
        """,
        (utcnow(), int(ok), error, "ok" if ok else "error", utcnow(), integration_id),
    )


def mark_reauth_required(conn: sqlite3.Connection, integration_id: int, message: str) -> None:
    conn.execute(
        "UPDATE integrations SET status = 'reauth_required', last_test_error = ?, updated_at = ? WHERE id = ?",
        (message, utcnow(), integration_id),
    )
