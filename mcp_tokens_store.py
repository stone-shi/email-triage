"""Per-user MCP bearer tokens (data/app.db, table ``mcp_tokens``).

Replaces the old ``EMAIL_TRIAGE_PROFILE_TOKEN`` line scraped out of each
profile's ``.env`` by ``generate_profile_token.py``/``load_token_profile_map``.
A table rather than a column on ``users``: rotation needs old+new valid
simultaneously, and one user legitimately drives several MCP clients (Claude
Desktop, an editor, a script), each wanting its own revocable token.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from appdb import utcnow
from app_errors import NotFoundError
from security import hash_mcp_token, new_mcp_token


def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "token_prefix": row["token_prefix"],
        "label": row["label"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }


def list_tokens(conn: sqlite3.Connection, user_id: int, *, include_revoked: bool = False) -> List[sqlite3.Row]:
    where = "" if include_revoked else "AND revoked_at IS NULL"
    return conn.execute(
        f"SELECT * FROM mcp_tokens WHERE user_id = ? {where} ORDER BY created_at DESC", (user_id,)
    ).fetchall()


def create_token(conn: sqlite3.Connection, user_id: int, *, label: Optional[str] = None) -> tuple:
    """Returns ``(raw_token, row)``. The raw token is never retrievable again."""
    raw = new_mcp_token()
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO mcp_tokens (user_id, token_hash, token_prefix, label, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, hash_mcp_token(raw), raw[:6], label, now),
    )
    row = conn.execute("SELECT * FROM mcp_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
    return raw, row


def import_token(conn: sqlite3.Connection, user_id: int, raw_token: str, *, label: str = "migrated from .env") -> sqlite3.Row:
    """Insert a pre-existing raw token value (migration path) so live MCP
    clients holding it keep working without reconfiguration."""
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO mcp_tokens (user_id, token_hash, token_prefix, label, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(token_hash) DO NOTHING
        """,
        (user_id, hash_mcp_token(raw_token), raw_token[:6], label, now),
    )
    if cur.lastrowid:
        return conn.execute("SELECT * FROM mcp_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
    return conn.execute("SELECT * FROM mcp_tokens WHERE token_hash = ?", (hash_mcp_token(raw_token),)).fetchone()


def revoke_token(conn: sqlite3.Connection, user_id: int, token_id: int) -> None:
    row = conn.execute(
        "SELECT * FROM mcp_tokens WHERE id = ? AND user_id = ?", (token_id, user_id)
    ).fetchone()
    if row is None:
        raise NotFoundError("MCP token not found")
    conn.execute("UPDATE mcp_tokens SET revoked_at = ? WHERE id = ?", (utcnow(), token_id))


def resolve_token(conn: sqlite3.Connection, raw_token: str) -> Optional[sqlite3.Row]:
    """Look up the (non-revoked) row for a raw bearer token, or None."""
    return conn.execute(
        "SELECT * FROM mcp_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (hash_mcp_token(raw_token),),
    ).fetchone()


def touch_last_used(conn: sqlite3.Connection, token_id: int, *, min_interval_seconds: int = 60) -> None:
    """Stamp last_used_at, at most once per interval to avoid a write per MCP call."""
    row = conn.execute("SELECT last_used_at FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        return
    now = datetime.now(timezone.utc)
    if row["last_used_at"]:
        last = datetime.fromisoformat(row["last_used_at"])
        if (now - last).total_seconds() < min_interval_seconds:
            return
    conn.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (now.isoformat(), token_id))
