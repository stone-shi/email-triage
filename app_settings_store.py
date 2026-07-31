"""Global (app_settings) and per-user (user_settings) runtime configuration.

Registry-driven, mirroring my-meeting-notes' app/config.py RUNTIME_KEYS shape:
one entry per key describing its type, whether it's a secret (Fernet-encrypted
via secretstore, same as integration credentials), and which dotted attribute
path on config.Settings it overlays. config.py::apply_db_settings is the only
other module that needs to know this mapping exists.

Precedence (see config.py): DB (this module) > env vars > YAML. That is a
deliberate inversion of the historical "env always wins over YAML" rule --
required so the admin System Settings UI actually takes effect on a box whose
.env already sets e.g. the LLM base URLs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import secretstore
from appdb import utcnow
from app_errors import ValidationError


@dataclass(frozen=True)
class SettingSpec:
    key: str
    value_type: str  # 'str' | 'int' | 'float' | 'bool' | 'json'
    attr_path: str  # dotted path onto a Settings instance, e.g. "triage.confidence_threshold"
    is_secret: bool = False
    user_overridable: bool = False


# One row per key. `attr_path` matches config.py's Settings/TriageSettings/etc. field names.
RUNTIME_KEYS: Dict[str, SettingSpec] = {
    spec.key: spec
    for spec in [
        # LLM endpoints -- global only (models/providers are an operator decision)
        SettingSpec("triage_base_url", "str", "triage_base_url"),
        SettingSpec("triage_model", "str", "triage_model"),
        SettingSpec("triage_api_key", "str", "triage_api_key", is_secret=True),
        SettingSpec("summary_base_url", "str", "summary_base_url"),
        SettingSpec("summary_model", "str", "summary_model"),
        SettingSpec("summary_api_key", "str", "summary_api_key", is_secret=True),
        # Triage router -- global, except sender lists + confidence_threshold (per-user)
        SettingSpec("triage.confidence_threshold", "float", "triage.confidence_threshold", user_overridable=True),
        SettingSpec("triage.triage_type", "str", "triage.triage_type"),
        SettingSpec("tei_url", "str", "tei_url"),
        SettingSpec("tei_model", "str", "tei_model"),
        SettingSpec("tei_api_key", "str", "tei_api_key", is_secret=True),
        SettingSpec("triage.tei_router_enabled", "bool", "triage.tei_router_enabled"),
        SettingSpec("triage.tei_noise_enabled", "bool", "triage.tei_noise_enabled"),
        SettingSpec("triage.tei_signal_enabled", "bool", "triage.tei_signal_enabled"),
        SettingSpec("triage.tei_noise_threshold", "float", "triage.tei_noise_threshold"),
        SettingSpec("triage.tei_signal_threshold", "float", "triage.tei_signal_threshold"),
        # Sender lists -- per-user (personal: my VIP isn't yours)
        SettingSpec("triage.whitelist_vip_senders", "json", "triage.whitelist_vip_senders", user_overridable=True),
        SettingSpec("triage.whitelist_domains", "json", "triage.whitelist_domains", user_overridable=True),
        SettingSpec("triage.blacklist_keywords", "json", "triage.blacklist_keywords", user_overridable=True),
        SettingSpec("triage.blacklist_senders", "json", "triage.blacklist_senders", user_overridable=True),
        # Scheduler -- global (process-wide already)
        SettingSpec("scheduler.enabled", "bool", "scheduler.enabled"),
        SettingSpec("scheduler.interval", "str", "scheduler.interval"),
        SettingSpec("scheduler.max_per_account", "int", "scheduler.max_per_account"),
        SettingSpec("scheduler.days", "int", "scheduler.days"),
        SettingSpec("download_all_scheduler.enabled", "bool", "download_all_scheduler.enabled"),
        SettingSpec("download_all_scheduler.interval", "str", "download_all_scheduler.interval"),
        # Auto mark read -- global
        SettingSpec("auto_mark_read.level_0.enabled", "bool", "auto_mark_read.level_0.enabled"),
        SettingSpec("auto_mark_read.level_0.after_displays", "int", "auto_mark_read.level_0.after_displays"),
        SettingSpec("auto_mark_read.level_1.enabled", "bool", "auto_mark_read.level_1.enabled"),
        SettingSpec("auto_mark_read.level_1.after_displays", "int", "auto_mark_read.level_1.after_displays"),
        SettingSpec("auto_mark_read.level_2.enabled", "bool", "auto_mark_read.level_2.enabled"),
        SettingSpec("auto_mark_read.level_2.after_displays", "int", "auto_mark_read.level_2.after_displays"),
        # Logging / deployment
        SettingSpec("log_level", "str", "log_level"),
        SettingSpec("public_base_url", "str", "public_base_url"),
        # OAuth client config (app-level, used for new Gmail/Zoho connections)
        SettingSpec("google_client_id", "str", "google_client_id"),
        SettingSpec("google_client_secret", "str", "google_client_secret", is_secret=True),
        SettingSpec("zoho_client_id", "str", "zoho_client_id"),
        SettingSpec("zoho_client_secret", "str", "zoho_client_secret", is_secret=True),
        SettingSpec("zoho_dc", "str", "zoho_dc"),
    ]
}


def _serialize(value: Any, value_type: str) -> str:
    if value_type == "bool":
        return "true" if value else "false"
    if value_type == "json":
        return json.dumps(value)
    return str(value)


def _deserialize(raw: str, value_type: str) -> Any:
    if value_type == "bool":
        return raw.strip().lower() in ("true", "1", "yes")
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "json":
        return json.loads(raw)
    return raw


def _set_attr_path(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def _get_attr_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


# --------------------------------------------------------------------------- #
# Global app_settings
# --------------------------------------------------------------------------- #


def get_raw(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM app_settings WHERE key = ?", (key,)).fetchone()


def get_value(conn: sqlite3.Connection, key: str) -> Any:
    """Decoded value for one key, or None if unset."""
    spec = RUNTIME_KEYS.get(key)
    row = get_raw(conn, key)
    if row is None or row["value"] is None:
        return None
    value_type = spec.value_type if spec else row["value_type"]
    if spec and spec.is_secret:
        decrypted = secretstore.decrypt(row["value"])
        return decrypted.get("value")
    return _deserialize(row["value"], value_type)


def set_value(conn: sqlite3.Connection, key: str, value: Any, *, updated_by: Optional[int] = None) -> None:
    spec = RUNTIME_KEYS.get(key)
    if spec is None:
        raise ValidationError(f"Unknown setting key {key!r}")
    stored = secretstore.encrypt({"value": value}) if spec.is_secret else _serialize(value, spec.value_type)
    conn.execute(
        """
        INSERT INTO app_settings (key, value, value_type, is_secret, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, value_type=excluded.value_type,
            is_secret=excluded.is_secret, updated_by=excluded.updated_by, updated_at=excluded.updated_at
        """,
        (key, stored, spec.value_type, int(spec.is_secret), updated_by, utcnow()),
    )


def set_many(conn: sqlite3.Connection, values: Dict[str, Any], *, updated_by: Optional[int] = None) -> List[str]:
    unknown = [k for k in values if k not in RUNTIME_KEYS]
    if unknown:
        raise ValidationError(f"Unknown setting keys: {', '.join(unknown)}")
    for key, value in values.items():
        set_value(conn, key, value, updated_by=updated_by)
    return list(values.keys())


def get_all_for_api(conn: sqlite3.Connection) -> Dict[str, dict]:
    """Every registered key, secrets masked, for the admin settings page."""
    out = {}
    for key, spec in RUNTIME_KEYS.items():
        row = get_raw(conn, key)
        if row is None or row["value"] is None:
            out[key] = {"value": None, "type": spec.value_type, "is_secret": spec.is_secret, "set": False}
            continue
        if spec.is_secret:
            out[key] = {"value": "••••", "type": spec.value_type, "is_secret": True, "set": True}
        else:
            out[key] = {
                "value": _deserialize(row["value"], spec.value_type),
                "type": spec.value_type,
                "is_secret": False,
                "set": True,
            }
    return out


# --------------------------------------------------------------------------- #
# Per-user overrides
# --------------------------------------------------------------------------- #


def get_user_value(conn: sqlite3.Connection, user_id: int, key: str) -> Any:
    spec = RUNTIME_KEYS.get(key)
    row = conn.execute(
        "SELECT * FROM user_settings WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()
    if row is None or row["value"] is None:
        return None
    return _deserialize(row["value"], spec.value_type if spec else row["value_type"])


def set_user_value(conn: sqlite3.Connection, user_id: int, key: str, value: Any) -> None:
    spec = RUNTIME_KEYS.get(key)
    if spec is None:
        raise ValidationError(f"Unknown setting key {key!r}")
    if not spec.user_overridable:
        raise ValidationError(f"{key!r} is a global (admin-only) setting, not per-user")
    conn.execute(
        """
        INSERT INTO user_settings (user_id, key, value, value_type, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, value_type=excluded.value_type,
            updated_at=excluded.updated_at
        """,
        (user_id, key, _serialize(value, spec.value_type), spec.value_type, utcnow()),
    )


def get_user_overrides_for_api(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    return {
        key: get_user_value(conn, user_id, key)
        for key, spec in RUNTIME_KEYS.items()
        if spec.user_overridable
    }


# --------------------------------------------------------------------------- #
# Applying onto a Settings instance
# --------------------------------------------------------------------------- #


def apply_to_settings(conn: sqlite3.Connection, settings_obj: Any, *, user_id: Optional[int] = None) -> None:
    """Overlay app_settings (global), then user_settings (per-user overridable
    keys only), onto an already-constructed Settings instance. Called as the
    final step of config.Settings.load_for_user -- these values win over both
    YAML and env vars."""
    for key, spec in RUNTIME_KEYS.items():
        value = get_value(conn, key)
        if value is not None:
            _set_attr_path(settings_obj, spec.attr_path, value)
    if user_id is not None:
        for key, spec in RUNTIME_KEYS.items():
            if not spec.user_overridable:
                continue
            value = get_user_value(conn, user_id, key)
            if value is not None:
                _set_attr_path(settings_obj, spec.attr_path, value)
