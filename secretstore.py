"""Encryption for per-user integration credentials (and global API keys) at rest.

A Gmail/Zoho refresh token or an IMAP password grants standing access to
somebody's real mailbox. Those get encrypted with Fernet so that a copy of
``data/app.db`` is not a copy of every connected account.

Be honest about the threat model. Unless ``EMAIL_TRIAGE_SECRET_KEY`` is set the
key lives in the same bind-mounted volume as the database, so this protects a
stolen database dump and not much else. That is still worth having, because a
dump is by far the most likely way the file leaves the box.

Named ``secretstore`` rather than ``secrets`` so it cannot be confused with the
stdlib module of that name, which ``security.py`` uses for token minting.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

KEY_FILENAME = "secret.key"
DEFAULT_DATA_DIR = Path(__file__).parent.resolve() / "data"


class SecretKeyError(Exception):
    """The encryption key is missing, unreadable, or ambiguous.

    Deliberately fatal at startup. Guessing which of two candidate keys is the
    real one would silently render every stored credential undecryptable, and
    that failure would only surface later, one account at a time.
    """


class SecretDecryptError(Exception):
    """A stored secret could not be decrypted -- wrong key, or a tampered blob.

    Callers must catch this and mark the integration ``reauth_required``. A
    lost key has to degrade to "reconnect your accounts", never to a 500.
    """


# (key_id, fernet_key). Cached because every provider call decrypts something.
_cached: Optional[Tuple[str, bytes]] = None


def key_path(data_dir: Optional[Path] = None) -> Path:
    return (data_dir or DEFAULT_DATA_DIR) / KEY_FILENAME


def _normalise(material: str) -> bytes:
    """Accept a real Fernet key verbatim, else derive one from any string.

    Requiring base64 from whoever sets ``EMAIL_TRIAGE_SECRET_KEY`` is a
    footgun -- the error surfaces as an unrelated-looking ValueError deep
    inside Fernet. So a well-formed key is used as-is and anything else is
    hashed into the 32 bytes Fernet needs.
    """
    raw = material.strip()
    try:
        if len(base64.urlsafe_b64decode(raw.encode())) == 32:
            return raw.encode()
    except (ValueError, TypeError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _read_key_file(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SecretKeyError(
            f"{path} is mode {mode:04o} -- it must not be readable by group or other. "
            f"Fix it with: chmod 600 {path}"
        )
    return path.read_text(encoding="utf-8").strip()


def load_key(data_dir: Optional[Path] = None) -> Tuple[str, bytes]:
    """Resolve the encryption key, generating one on first run.

    Returns ``(key_id, key)``. ``key_id`` is recorded alongside each ciphertext
    so a future key rotation can tell which key a given blob belongs to.
    """
    global _cached
    if _cached is not None:
        return _cached

    path = key_path(data_dir)
    env_material = os.getenv("EMAIL_TRIAGE_SECRET_KEY", "").strip()
    file_material = _read_key_file(path) if path.exists() else None

    if env_material and file_material and _normalise(env_material) != _normalise(file_material):
        raise SecretKeyError(
            f"EMAIL_TRIAGE_SECRET_KEY is set, and {path} holds a different key. Remove "
            "whichever one is stale -- silently picking either would make every "
            "credential encrypted under the other permanently unreadable."
        )

    if env_material:
        material = env_material
    elif file_material:
        material = file_material
    else:
        material = Fernet.generate_key().decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT|O_EXCL with mode 0600: writing first and chmod-ing after would
        # leave the key world-readable for the window in between.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(material)

    key = _normalise(material)
    _cached = (hashlib.sha256(key).hexdigest()[:8], key)
    return _cached


def reset_key_cache() -> None:
    """Drop the cached key. Tests use this after moving the data dir."""
    global _cached
    _cached = None


def encrypt(payload: dict, data_dir: Optional[Path] = None) -> str:
    """Serialise and encrypt a credential dict into a storable envelope."""
    key_id, key = load_key(data_dir)
    token = Fernet(key).encrypt(json.dumps(payload, separators=(",", ":")).encode())
    return json.dumps({"key_id": key_id, "ct": token.decode()}, separators=(",", ":"))


def decrypt(blob: Optional[str], data_dir: Optional[Path] = None) -> dict:
    """Inverse of :func:`encrypt`. Empty input is an empty dict, not an error."""
    if not blob:
        return {}

    try:
        envelope = json.loads(blob)
        token = envelope["ct"]
    except (ValueError, TypeError, KeyError) as exc:
        raise SecretDecryptError("Stored credential is not a valid secret envelope") from exc

    key_id, key = load_key(data_dir)
    stored_id = envelope.get("key_id")
    try:
        plain = Fernet(key).decrypt(token.encode())
    except (InvalidToken, TypeError, ValueError) as exc:
        # Naming both key ids turns "it just broke" into an actionable message.
        hint = (
            f" It was encrypted under key {stored_id}, but the key in use is {key_id}."
            if stored_id and stored_id != key_id
            else ""
        )
        raise SecretDecryptError(f"Stored credential could not be decrypted.{hint}") from exc

    return json.loads(plain)
