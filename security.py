"""Password hashing, session tokens, and MCP bearer tokens.

Hashing is stdlib ``hashlib.scrypt`` -- no passlib, no compiled bcrypt. The
per-user ``password_algo``/``password_params`` columns exist so the cost can be
raised later and hashes upgraded transparently on the next successful login.

Session tokens and MCP tokens are both opaque and stored hashed: the database
holds ``sha256(token)`` while the raw token only ever lives in a cookie (session)
or is shown once at creation time (MCP token). A dump of the app DB therefore
yields nothing an attacker can replay.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

DEFAULT_ALGO = "scrypt"
DEFAULT_PARAMS = "n=16384,r=8,p=1,dklen=32"
SALT_BYTES = 16
TOKEN_BYTES = 32


@dataclass(frozen=True)
class ScryptParams:
    n: int = 16384
    r: int = 8
    p: int = 1
    dklen: int = 32

    def encode(self) -> str:
        return f"n={self.n},r={self.r},p={self.p},dklen={self.dklen}"

    @classmethod
    def decode(cls, raw: str) -> "ScryptParams":
        values = {}
        for part in raw.split(","):
            key, _, value = part.partition("=")
            values[key.strip()] = int(value)
        return cls(
            n=values.get("n", 16384),
            r=values.get("r", 8),
            p=values.get("p", 1),
            dklen=values.get("dklen", 32),
        )


def _derive(password: str, salt: bytes, params: ScryptParams) -> str:
    # maxmem must be generous enough for n=16384,r=8 (~16 MiB) or scrypt raises.
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        dklen=params.dklen,
        maxmem=64 * 1024 * 1024,
    )
    return digest.hex()


def hash_password(password: str, params: Optional[ScryptParams] = None) -> dict:
    """Return the four column values that describe this password."""
    params = params or ScryptParams()
    salt = secrets.token_bytes(SALT_BYTES)
    return {
        "password_hash": _derive(password, salt, params),
        "password_salt": salt.hex(),
        "password_algo": DEFAULT_ALGO,
        "password_params": params.encode(),
    }


def verify_password(
    password: str,
    *,
    password_hash: str,
    password_salt: str,
    password_algo: str = DEFAULT_ALGO,
    password_params: str = DEFAULT_PARAMS,
) -> bool:
    if password_algo != DEFAULT_ALGO:
        return False
    try:
        params = ScryptParams.decode(password_params)
        candidate = _derive(password, bytes.fromhex(password_salt), params)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, password_hash)


def needs_rehash(password_params: str, password_algo: str = DEFAULT_ALGO) -> bool:
    """True when a stored hash was made with weaker settings than we now use."""
    if password_algo != DEFAULT_ALGO:
        return True
    try:
        return ScryptParams.decode(password_params) != ScryptParams()
    except (ValueError, TypeError):
        return True


# --------------------------------------------------------------------------- #
# Session tokens
# --------------------------------------------------------------------------- #


def new_session_token() -> str:
    """Raw token. Goes in the cookie and is never persisted."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The ``sessions.id`` for a raw session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# MCP bearer tokens
# --------------------------------------------------------------------------- #


def new_mcp_token() -> str:
    """Raw token, shown to the user exactly once at creation."""
    return secrets.token_hex(24)


def hash_mcp_token(token: str) -> str:
    """The ``mcp_tokens.token_hash`` for a raw MCP token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Password policy
# --------------------------------------------------------------------------- #


def validate_password(password: str, *, min_length: int = 10, current: Optional[str] = None) -> Optional[str]:
    """Return an error message, or None when the password is acceptable."""
    if len(password) < min_length:
        return f"Password must be at least {min_length} characters"
    if current is not None and password == current:
        return "New password must be different from the current one"
    return None
