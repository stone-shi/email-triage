"""User and session persistence against data/app.db.

Every function takes an open connection so callers control the transaction
boundary; nothing here opens or commits on its own (the caller uses
``appdb.get_conn()`` or manages the connection itself).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from appdb import utcnow
from app_errors import ConflictError, NotFoundError, ValidationError
from security import (
    hash_password,
    hash_token,
    needs_rehash,
    new_session_token,
    validate_password,
    verify_password,
)

# Extending the session on every request would mean a write per request. Once
# per this interval is plenty for a sliding window measured in days.
SLIDING_REFRESH_SECONDS = 600

DEFAULT_SESSION_TTL_HOURS = 336  # 14 days
DEFAULT_PASSWORD_MIN_LENGTH = 10


def session_ttl_hours() -> int:
    try:
        return int(os.getenv("EMAIL_TRIAGE_SESSION_TTL_HOURS", DEFAULT_SESSION_TTL_HOURS))
    except ValueError:
        return DEFAULT_SESSION_TTL_HOURS


# Backward-compat alias for in-module callers written before this was made public.
_session_ttl_hours = session_ttl_hours


def password_min_length() -> int:
    try:
        return int(os.getenv("EMAIL_TRIAGE_PASSWORD_MIN_LENGTH", DEFAULT_PASSWORD_MIN_LENGTH))
    except ValueError:
        return DEFAULT_PASSWORD_MIN_LENGTH


# Backward-compat alias for in-module callers written before this was made public.
_password_min_length = password_min_length


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def slugify_username(username: str) -> str:
    """Derive a filesystem-safe, immutable workspace slug from a username."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", username.strip().lower()).strip("-")
    return slug or "user"


def row_to_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "workspace_slug": row["workspace_slug"],
        "is_admin": bool(row["is_admin"]),
        "is_active": bool(row["is_active"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()


def resolve_user(conn: sqlite3.Connection, user: "int | str") -> Optional[sqlite3.Row]:
    """Accept either a user id or a username -- the two forms callers reasonably pass."""
    if isinstance(user, int):
        return get_user(conn, user)
    if isinstance(user, str) and user.isdigit():
        row = get_user(conn, int(user))
        if row is not None:
            return row
    return get_user_by_username(conn, str(user))


def list_users(
    conn: sqlite3.Connection, *, include_inactive: bool = False, page: int = 1, page_size: int = 50
) -> Tuple[List[sqlite3.Row], int]:
    where = "" if include_inactive else "WHERE is_active = 1"
    total = conn.execute(f"SELECT COUNT(*) FROM users {where}").fetchone()[0]
    offset = max(page - 1, 0) * page_size
    rows = conn.execute(
        f"SELECT * FROM users {where} ORDER BY username COLLATE NOCASE LIMIT ? OFFSET ?",
        (page_size, offset),
    ).fetchall()
    return rows, total


def list_active_users(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY username COLLATE NOCASE").fetchall()


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_active_admins(conn: sqlite3.Connection, exclude_id: Optional[int] = None) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1"
    params: list = []
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone()[0]


def _unique_workspace_slug(conn: sqlite3.Connection, username: str) -> str:
    base = slugify_username(username)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM users WHERE workspace_slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    display_name: Optional[str] = None,
    is_admin: bool = False,
    must_change_password: bool = True,
    workspace_slug: Optional[str] = None,
) -> sqlite3.Row:
    error = validate_password(password, min_length=_password_min_length())
    if error:
        raise ValidationError(error)

    if get_user_by_username(conn, username) is not None:
        raise ConflictError(f"Username {username!r} is already taken")

    slug = workspace_slug or _unique_workspace_slug(conn, username)
    now = utcnow()
    creds = hash_password(password)
    cur = conn.execute(
        """
        INSERT INTO users (username, display_name, workspace_slug, password_hash, password_salt,
                           password_algo, password_params, is_admin, is_active,
                           must_change_password, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            username,
            display_name,
            slug,
            creds["password_hash"],
            creds["password_salt"],
            creds["password_algo"],
            creds["password_params"],
            int(is_admin),
            int(must_change_password),
            now,
            now,
        ),
    )
    return get_user(conn, cur.lastrowid)


def update_user(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    display_name: Optional[str] = None,
    is_admin: Optional[bool] = None,
    is_active: Optional[bool] = None,
) -> sqlite3.Row:
    row = get_user(conn, user_id)
    if row is None:
        raise NotFoundError("User not found")

    deactivating = is_active is False and row["is_active"]
    demoting = bool(row["is_admin"]) and is_admin is False
    deactivating_admin = bool(row["is_admin"]) and deactivating
    if (demoting or deactivating_admin) and count_active_admins(conn, exclude_id=user_id) == 0:
        raise ConflictError("Cannot remove the last active administrator")

    fields, params = [], []
    if display_name is not None:
        fields.append("display_name = ?")
        params.append(display_name)
    if is_admin is not None:
        fields.append("is_admin = ?")
        params.append(int(is_admin))
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(int(is_active))
    if fields:
        fields.append("updated_at = ?")
        params.append(utcnow())
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)

    if deactivating:
        delete_other_sessions(conn, user_id, keep_session_id=None)

    return get_user(conn, user_id)


def delete_user(conn: sqlite3.Connection, user_id: int, *, requesting_user_id: int) -> None:
    """Soft-delete only (is_active=0). Never a hard DELETE -- a user may own a
    600MB email_cache.db and integration rows that must not be silently dropped."""
    if user_id == requesting_user_id:
        raise ConflictError("Cannot delete your own account")
    row = get_user(conn, user_id)
    if row is None:
        raise NotFoundError("User not found")
    if row["is_admin"] and count_active_admins(conn, exclude_id=user_id) == 0:
        raise ConflictError("Cannot remove the last active administrator")

    conn.execute("UPDATE users SET is_active = 0, updated_at = ? WHERE id = ?", (utcnow(), user_id))
    delete_other_sessions(conn, user_id, keep_session_id=None)


def set_password(conn: sqlite3.Connection, user_id: int, password: str, *, must_change: bool = False) -> None:
    creds = hash_password(password)
    conn.execute(
        """
        UPDATE users
           SET password_hash = ?, password_salt = ?, password_algo = ?,
               password_params = ?, must_change_password = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            creds["password_hash"],
            creds["password_salt"],
            creds["password_algo"],
            creds["password_params"],
            int(must_change),
            utcnow(),
            user_id,
        ),
    )


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> Optional[sqlite3.Row]:
    """Return the user row on success, else None. Inactive users never succeed."""
    row = get_user_by_username(conn, username)
    if row is None:
        # Spend comparable time on an unknown username so response time doesn't
        # reveal which accounts exist.
        verify_password(password, password_hash="0" * 64, password_salt="00" * 16)
        return None

    if not row["is_active"]:
        return None

    ok = verify_password(
        password,
        password_hash=row["password_hash"],
        password_salt=row["password_salt"],
        password_algo=row["password_algo"],
        password_params=row["password_params"],
    )
    if not ok:
        return None

    if needs_rehash(row["password_params"], row["password_algo"]):
        set_password(conn, row["id"], password, must_change=bool(row["must_change_password"]))
        row = get_user(conn, row["id"])

    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), row["id"]))
    return get_user(conn, row["id"])


def seed_admin(conn: sqlite3.Connection, *, username: str = "admin", password: str = "password") -> bool:
    """Create the bootstrap admin when the user table is empty.

    Idempotent: an existing user table is never touched, so changing the
    bootstrap password env var later does nothing to a live install.
    """
    if count_users(conn) > 0:
        return False

    now = utcnow()
    creds = hash_password(password)
    conn.execute(
        """
        INSERT INTO users (username, display_name, workspace_slug, password_hash, password_salt,
                           password_algo, password_params, is_admin, is_active,
                           must_change_password, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 1, ?, ?)
        """,
        (
            username,
            "Administrator",
            _unique_workspace_slug(conn, username),
            creds["password_hash"],
            creds["password_salt"],
            creds["password_algo"],
            creds["password_params"],
            now,
            now,
        ),
    )
    return True


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def create_session(
    conn: sqlite3.Connection, user_id: int, *, user_agent: Optional[str] = None, ip: Optional[str] = None
) -> Tuple[str, str]:
    """Create a session. Returns ``(raw_token, session_id)``."""
    token = new_session_token()
    session_id = hash_token(token)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=_session_ttl_hours())

    conn.execute(
        """
        INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at, user_agent, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, user_id, now.isoformat(), expires.isoformat(), now.isoformat(), user_agent, ip),
    )
    return token, session_id


def _reap(conn: sqlite3.Connection, session_id: str) -> None:
    """Drop an unusable session and commit immediately so the cleanup survives
    a request that's about to raise (and therefore roll back)."""
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def resolve_session(conn: sqlite3.Connection, token: str) -> Optional[Tuple[sqlite3.Row, sqlite3.Row]]:
    """Look up ``(session, user)`` for a raw token, or None if unusable.

    An expired session is deleted on sight rather than left to the cleanup job.
    """
    session_id = hash_token(token)
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        return None

    now = datetime.now(timezone.utc)
    if _parse(session["expires_at"]) <= now:
        _reap(conn, session_id)
        return None

    user = get_user(conn, session["user_id"])
    if user is None or not user["is_active"]:
        _reap(conn, session_id)
        return None

    return session, user


def touch_session(conn: sqlite3.Connection, session: sqlite3.Row) -> None:
    """Slide the expiry, at most once per SLIDING_REFRESH_SECONDS."""
    now = datetime.now(timezone.utc)
    last_seen = session["last_seen_at"]
    if last_seen and (now - _parse(last_seen)).total_seconds() < SLIDING_REFRESH_SECONDS:
        return

    expires = now + timedelta(hours=_session_ttl_hours())
    conn.execute(
        "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (now.isoformat(), expires.isoformat(), session["id"]),
    )


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def delete_other_sessions(conn: sqlite3.Connection, user_id: int, keep_session_id: Optional[str]) -> int:
    if keep_session_id:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, keep_session_id))
    else:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return cur.rowcount


def list_sessions(conn: sqlite3.Connection, user_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),))
    return cur.rowcount
