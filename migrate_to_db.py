#!/usr/bin/env python3
"""One-time (idempotent) import of the filesystem-based profiles/config.yml/.env
world into data/app.db: global settings, per-profile users, their Gmail/IMAP
integrations, and their MCP bearer tokens.

Non-destructive by construction: nothing on disk is deleted or modified.
.env/config.yml/token.json/google_cli_client.json remain exactly where they
are, both as the lower layer of config.py's resolution cascade and as the
rollback path (`rm data/app.db` + revert code gets you back to where you
started).

Guarded by an app_settings marker row -- once a real (non-dry-run) run
completes, re-running this is a no-op. --dry-run initializes the app.db
schema (harmless, idempotent) but writes no user/integration/settings rows,
so it can be inspected and re-run freely before committing to the real thing.

Usage:
    ./venv/bin/python3 migrate_to_db.py --dry-run
    ./venv/bin/python3 migrate_to_db.py
"""

from __future__ import annotations

import argparse
import json
import secrets as _secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import appdb
import app_settings_store
import integrations_store as ints
import mcp_tokens_store
import secretstore
import users_store as us
from config import PLACEHOLDER_GMAIL_ACCOUNT, PLACEHOLDER_IMAP_LOGIN, Settings

WORKSPACE_ROOT = Path(__file__).parent.resolve()
MARKER_KEY = "bootstrap_imported_v1"
DEFAULT_ADMIN_USERNAME = "admin"
# Profiles that also become admins during migration, if they exist -- 'stone's
# root .env holds this deployment's global LLM config, i.e. stone is the de
# facto operator, so a fresh admin/<random password> shouldn't be the only
# account that can administer the system.
DEFAULT_EXTRA_ADMIN_USERNAMES = ("stone",)


def _get_attr_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def _real_profile_dirs() -> List[str]:
    """Every profiles/<name>/ that looks like it was actually configured (has
    a .env, config.yml, or token.json) -- excludes the empty phantom
    'default' directory that list_profile_names() always includes."""
    profiles_dir = WORKSPACE_ROOT / "profiles"
    if not profiles_dir.exists():
        return []
    names = []
    for entry in sorted(profiles_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / ".env").exists() or (entry / "config.yml").exists() or (entry / "token.json").exists():
            names.append(entry.name)
    return names


def _get_marker(conn) -> bool:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (MARKER_KEY,)).fetchone()
    return row is not None and row["value"] == "1"


def _set_marker(conn) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) VALUES (?, '1', 'bool', 0, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
        (MARKER_KEY, appdb.utcnow()),
    )


def _import_global_settings(conn, report: Dict[str, Any], *, dry_run: bool) -> None:
    """Every RUNTIME_KEYS value that differs from the bare pydantic default,
    resolved from data/config.yml + profiles/config-local.yml + root .env
    (Settings.load_for_user(None) -- no DB overlay exists yet at this point)."""
    global_settings = Settings.load_for_user(None)
    bare_defaults = Settings(_env_file=None)

    written = []
    for key, spec in app_settings_store.RUNTIME_KEYS.items():
        try:
            value = _get_attr_path(global_settings, spec.attr_path)
            default_value = _get_attr_path(bare_defaults, spec.attr_path)
        except AttributeError:
            continue
        if value in (None, "", []) or value == default_value:
            continue
        if not dry_run:
            app_settings_store.set_value(conn, key, value)
        written.append(key)
    report["settings_keys"] = written


def _import_admin_users(
    conn, report: Dict[str, Any], *, admin_username: str, extra_admin_usernames: List[str], dry_run: bool
) -> None:
    if us.count_users(conn) > 0:
        return
    password = _secrets.token_urlsafe(12)
    if not dry_run:
        us.seed_admin(conn, username=admin_username, password=password)
    report["temp_passwords"][admin_username] = password
    report["users"].append({"username": admin_username, "is_admin": True, "workspace_slug": admin_username})


def _import_profile_users(
    conn, report: Dict[str, Any], *, extra_admin_usernames: List[str], dry_run: bool
) -> Dict[str, int]:
    """Returns {profile_name: user_id} (-1 placeholder ids under --dry-run)."""
    user_ids: Dict[str, int] = {}
    for name in _real_profile_dirs():
        existing = us.get_user_by_username(conn, name)
        if existing is not None:
            user_ids[name] = existing["id"]
            continue
        password = _secrets.token_urlsafe(12)
        is_admin = name in extra_admin_usernames
        if not dry_run:
            row = us.create_user(
                conn, username=name, password=password, is_admin=is_admin,
                must_change_password=True, workspace_slug=name,
            )
            user_ids[name] = row["id"]
        else:
            user_ids[name] = -1
        report["temp_passwords"][name] = password
        report["users"].append({"username": name, "is_admin": is_admin, "workspace_slug": name})
    return user_ids


def _import_gmail_integration(conn, name: str, user_id: int, report: Dict[str, Any], *, dry_run: bool) -> None:
    profile_dir = WORKSPACE_ROOT / "profiles" / name
    token_path = profile_dir / "token.json"
    if not token_path.exists():
        return
    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as e:
        report["warnings"].append(f"{name}: failed to read token.json: {e}")
        return

    env = _read_env_file(profile_dir / ".env") or _read_env_file(WORKSPACE_ROOT / ".env")
    gmail_account = env.get("EMAIL_TRIAGE_GMAIL_ACCOUNT", "").strip().lower()
    if not gmail_account or gmail_account == PLACEHOLDER_GMAIL_ACCOUNT:
        report["warnings"].append(f"{name}: token.json present but EMAIL_TRIAGE_GMAIL_ACCOUNT is not set; skipping Gmail import")
        return

    creds_path_name = Path(env.get("EMAIL_TRIAGE_GMAIL_CREDENTIALS_PATH", "google_cli_client.json")).name
    creds_path = profile_dir / creds_path_name
    client_id = client_secret = None
    if creds_path.exists():
        try:
            raw = json.loads(creds_path.read_text(encoding="utf-8"))
            block = raw.get("installed") or raw.get("web") or raw
            client_id = block.get("client_id")
            client_secret = block.get("client_secret")
        except Exception as e:
            report["warnings"].append(f"{name}: failed to read {creds_path_name}: {e}")
    else:
        report["warnings"].append(f"{name}: {creds_path_name} not found; Gmail token refresh will fail until reconnected")

    secret = {
        "access_token": token_data.get("token"),
        "refresh_token": token_data.get("refresh_token"),
        "token_uri": token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if not dry_run:
        row = ints.create_integration(
            conn, user_id=user_id, provider="gmail", account_key=gmail_account, auth_type="oauth",
            account_label=gmail_account, secret=secret,
            scopes=" ".join(token_data.get("scopes", [])) or None,
            status="ok", token_expires_at=token_data.get("expiry"),
            config={"legacy_token_path": str(token_path), "legacy_client_path": str(creds_path)},
        )
        report["integration_ids"][(name, "gmail", gmail_account)] = row["id"]
    report["integrations"].append({"profile": name, "provider": "gmail", "account_key": gmail_account})


def _import_imap_integration(conn, name: str, user_id: int, report: Dict[str, Any], *, dry_run: bool) -> None:
    profile_dir = WORKSPACE_ROOT / "profiles" / name
    env = _read_env_file(profile_dir / ".env") or _read_env_file(WORKSPACE_ROOT / ".env")
    imap_login = env.get("EMAIL_TRIAGE_IMAP_LOGIN", "").strip().lower()
    imap_password = env.get("EMAIL_TRIAGE_IMAP_PASSWORD", "")
    if not imap_login or imap_login == PLACEHOLDER_IMAP_LOGIN or not imap_password:
        return

    secret = {"password": imap_password}
    smtp_password = env.get("EMAIL_TRIAGE_SMTP_PASSWORD")
    if smtp_password:
        secret["smtp_password"] = smtp_password

    config = {
        "host": env.get("EMAIL_TRIAGE_IMAP_HOST", "imap.zoho.com"),
        "port": int(env.get("EMAIL_TRIAGE_IMAP_PORT", "993")),
        "smtp_host": env.get("EMAIL_TRIAGE_SMTP_HOST", "smtp.zoho.com"),
        "smtp_port": int(env.get("EMAIL_TRIAGE_SMTP_PORT", "465")),
        "smtp_login": env.get("EMAIL_TRIAGE_SMTP_LOGIN") or imap_login,
    }

    if not dry_run:
        row = ints.create_integration(
            conn, user_id=user_id, provider="imap", account_key=imap_login, auth_type="password",
            account_label=imap_login, secret=secret, status="ok", config=config,
        )
        report["integration_ids"][(name, "imap", imap_login)] = row["id"]
    report["integrations"].append({"profile": name, "provider": "imap", "account_key": imap_login})


def _import_mcp_token(conn, name: str, user_id: int, report: Dict[str, Any], *, dry_run: bool) -> None:
    profile_dir = WORKSPACE_ROOT / "profiles" / name
    env = _read_env_file(profile_dir / ".env")
    token = env.get("EMAIL_TRIAGE_PROFILE_TOKEN")
    if not token:
        return
    if not dry_run:
        mcp_tokens_store.import_token(conn, user_id, token, label="migrated from .env")
    report["mcp_tokens"] += 1


def _import_admin_mcp_token(conn, admin_user_id: int, report: Dict[str, Any], *, dry_run: bool) -> None:
    env = _read_env_file(WORKSPACE_ROOT / ".env")
    token = env.get("EMAIL_TRIAGE_PROFILE_TOKEN")
    if not token:
        return
    if not dry_run:
        mcp_tokens_store.import_token(conn, admin_user_id, token, label="migrated from root .env")
    report["mcp_tokens"] += 1


def _backfill_email_cache(name: str, report: Dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    from db import EmailDB

    profile_settings = Settings.load_for_profile(name)
    email_db = EmailDB(settings_instance=profile_settings)
    for (profile_name, provider, account_key), integration_id in report["integration_ids"].items():
        if profile_name != name:
            continue
        updated = email_db.backfill_integration(account_key, integration_id, provider)
        report["backfilled_rows"] = report.get("backfilled_rows", 0) + updated


def bootstrap(
    db_path: Optional[Path] = None,
    *,
    dry_run: bool = False,
    admin_username: str = DEFAULT_ADMIN_USERNAME,
    extra_admin_usernames: Optional[List[str]] = None,
) -> Dict[str, Any]:
    extra_admin_usernames = list(extra_admin_usernames if extra_admin_usernames is not None else DEFAULT_EXTRA_ADMIN_USERNAMES)
    resolved_path = db_path or appdb.DEFAULT_APP_DB_PATH

    report: Dict[str, Any] = {
        "dry_run": dry_run,
        "db_path": str(resolved_path),
        "already_imported": False,
        "users": [],
        "integrations": [],
        "integration_ids": {},
        "temp_passwords": {},
        "mcp_tokens": 0,
        "settings_keys": [],
        "warnings": [],
    }

    appdb.init_app_db(resolved_path)
    if not dry_run:
        secretstore.load_key()

    with appdb.get_conn(resolved_path) as conn:
        if _get_marker(conn):
            report["already_imported"] = True
            return report

        _import_global_settings(conn, report, dry_run=dry_run)
        _import_admin_users(conn, report, admin_username=admin_username, extra_admin_usernames=extra_admin_usernames, dry_run=dry_run)

        user_ids = _import_profile_users(conn, report, extra_admin_usernames=extra_admin_usernames, dry_run=dry_run)

        for name, user_id in user_ids.items():
            _import_gmail_integration(conn, name, user_id, report, dry_run=dry_run)
            _import_imap_integration(conn, name, user_id, report, dry_run=dry_run)
            _import_mcp_token(conn, name, user_id, report, dry_run=dry_run)

        admin_row = us.get_user_by_username(conn, admin_username)
        if admin_row is not None:
            _import_admin_mcp_token(conn, admin_row["id"], report, dry_run=dry_run)

        if not dry_run:
            _set_marker(conn)

    for name in user_ids:
        _backfill_email_cache(name, report, dry_run=dry_run)

    return report


def _print_report(report: Dict[str, Any]) -> None:
    if report["already_imported"]:
        print("Already imported -- nothing to do (data/app.db has the bootstrap_imported_v1 marker set).")
        return

    mode = "DRY RUN -- nothing was written" if report["dry_run"] else "COMPLETE"
    print(f"=== migrate_to_db.py: {mode} ===")
    print(f"App DB: {report['db_path']}")
    print(f"Global settings imported: {len(report['settings_keys'])} keys")
    print(f"Users: {len(report['users'])}")
    for u in report["users"]:
        admin_note = " (admin)" if u["is_admin"] else ""
        print(f"  - {u['username']}{admin_note}  [workspace: profiles/{u['workspace_slug']}/]")
    print(f"Integrations: {len(report['integrations'])}")
    for i in report["integrations"]:
        print(f"  - {i['profile']}: {i['provider']} -> {i['account_key']}")
    print(f"MCP tokens imported: {report['mcp_tokens']}")
    if report.get("backfilled_rows"):
        print(f"email_cache rows backfilled with integration_id: {report['backfilled_rows']}")
    if report["warnings"]:
        print("Warnings:")
        for w in report["warnings"]:
            print(f"  ! {w}")
    if report["temp_passwords"]:
        print("\nTemporary passwords (shown once -- each user must change theirs at first login):")
        for username, password in report["temp_passwords"].items():
            print(f"  {username}: {password}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything")
    parser.add_argument("--admin-username", default=DEFAULT_ADMIN_USERNAME, help="Bootstrap admin username")
    parser.add_argument(
        "--also-admin", action="append", default=None, metavar="USERNAME",
        help="Additional profile(s) to promote to admin during import (repeatable). "
             f"Defaults to {list(DEFAULT_EXTRA_ADMIN_USERNAMES)!r} if that profile exists.",
    )
    args = parser.parse_args()

    report = bootstrap(
        dry_run=args.dry_run,
        admin_username=args.admin_username,
        extra_admin_usernames=args.also_admin,
    )
    _print_report(report)


if __name__ == "__main__":
    main()
