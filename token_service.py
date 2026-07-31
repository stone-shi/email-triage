"""OAuth access-token refresh for integrations (Gmail, Zoho), with a lock +
lease + compare-and-swap so a lost/expired grant degrades to "reconnect this
account" rather than a mailbox silently going stale.

Synchronous (this codebase's sync engine is threading, not asyncio). Provider-
specific refresh HTTP calls live in oauth_google.py/oauth_zoho.py (imported
lazily here to avoid a hard dependency at import time before those modules
exist) -- this module only owns the caching/locking/persistence around them.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Dict

import appdb
import integrations_store
import secretstore

REFRESH_MARGIN_SEC = 120

_locks: Dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


class TokenRefreshError(Exception):
    """The refresh call itself failed (network error, provider 5xx, etc).

    Transient -- callers should treat this like any other sync-tick failure,
    not mark the integration reauth_required.
    """


class ReauthRequired(Exception):
    """The provider rejected the refresh token outright (revoked/expired
    grant, e.g. Google's invalid_grant or Zoho evicting an old token past its
    per-client cap). Terminal -- the integration is marked reauth_required and
    won't be retried until the user reconnects it."""


def _lock_for(integration_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(integration_id)
        if lock is None:
            lock = threading.Lock()
            _locks[integration_id] = lock
        return lock


def _provider_refresh(provider: str, secret: dict) -> dict:
    """Dispatch to the provider-specific OAuth refresh call. Returns a dict of
    the fields to merge into the stored secret (at minimum `access_token`, and
    `refresh_token` only if the provider issued a new one), plus `expires_at`
    (ISO8601 string) for token_expires_at."""
    if provider == "gmail":
        import oauth_google

        return oauth_google.refresh(secret)
    if provider == "zoho":
        import oauth_zoho

        return oauth_zoho.refresh(secret)
    raise TokenRefreshError(f"No OAuth refresh implementation for provider {provider!r}")


def _is_fresh(row) -> bool:
    if not row["token_expires_at"]:
        return False
    try:
        expires = datetime.fromisoformat(row["token_expires_at"])
    except ValueError:
        return False
    return datetime.now(timezone.utc) < expires - timedelta(seconds=REFRESH_MARGIN_SEC)


def access_token(integration_id: int) -> str:
    """Returns a live access token for this integration, refreshing if needed.

    Three layers, because losing a grant orphans a mailbox until the user
    reconnects: (1) a per-integration threading.Lock so concurrent callers in
    this process serialize instead of racing a duplicate refresh; (2) a
    double-checked re-read of the row once inside the lock, in case another
    thread already refreshed while this one waited; (3) a secret_version
    compare-and-swap on write, so a second process that refreshed the same
    integration concurrently doesn't have its result silently overwritten --
    on a lost CAS, this call's freshly-fetched token is discarded in favor of
    reading back whatever the other writer stored.
    """
    with _lock_for(integration_id):
        with appdb.get_conn() as conn:
            row = integrations_store.get_integration(conn, integration_id)
            if row is None:
                raise TokenRefreshError(f"Integration {integration_id} not found")
            secret = integrations_store.get_secret(row)
            if _is_fresh(row) and secret.get("access_token"):
                return secret["access_token"]

            try:
                updated = _provider_refresh(row["provider"], secret)
            except ReauthRequired as exc:
                integrations_store.mark_reauth_required(conn, integration_id, str(exc))
                # get_conn()'s commit-on-clean-exit is skipped when we re-raise
                # through it, so commit explicitly or this write is silently lost.
                conn.commit()
                raise

            merged_secret = {**secret, **updated}
            cur = conn.execute(
                """
                UPDATE integrations
                   SET secret_json = ?, secret_version = secret_version + 1,
                       token_expires_at = ?, status = 'ok', last_test_error = NULL, updated_at = ?
                 WHERE id = ? AND secret_version = ?
                """,
                (
                    secretstore.encrypt(merged_secret),
                    updated.get("expires_at"),
                    appdb.utcnow(),
                    integration_id,
                    row["secret_version"],
                ),
            )
            if cur.rowcount == 0:
                # Someone else refreshed concurrently -- use their result
                # rather than overwriting it with ours.
                fresh_row = integrations_store.get_integration(conn, integration_id)
                fresh_secret = integrations_store.get_secret(fresh_row)
                return fresh_secret.get("access_token", merged_secret["access_token"])
            return merged_secret["access_token"]
