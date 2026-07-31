"""App-level database: users, sessions, MCP tokens, integrations, global/user settings.

Separate from db.py's per-user email_cache.db -- this one file (data/app.db) is
shared across every user of the deployment. Same style as db.py/my-meeting-notes'
app/db.py: raw sqlite3, no ORM, a SCHEMA tuple of idempotent CREATE TABLE
statements plus a LATE_COLUMNS list for additive migrations added after first
release, guarded by try/except so a column that already exists silently no-ops.
"""

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("email_triage.appdb")

DEFAULT_APP_DB_PATH = Path(__file__).parent.resolve() / "data" / "app.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT,
        workspace_slug TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        password_algo TEXT NOT NULL DEFAULT 'scrypt',
        password_params TEXT NOT NULL DEFAULT 'n=16384,r=8,p=1,dklen=32',
        is_admin INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen_at TEXT,
        user_agent TEXT,
        ip TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS mcp_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        token_prefix TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id, revoked_at)",
    """
    CREATE TABLE IF NOT EXISTS integrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        account_key TEXT NOT NULL,
        account_label TEXT,
        cache_account_key TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        triage_enabled INTEGER NOT NULL DEFAULT 1,
        archive_enabled INTEGER NOT NULL DEFAULT 1,
        auth_type TEXT NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{}',
        secret_json TEXT,
        secret_version INTEGER NOT NULL DEFAULT 1,
        scopes TEXT,
        token_expires_at TEXT,
        refresh_token_obtained_at TEXT,
        refresh_lease_until TEXT,
        status TEXT NOT NULL DEFAULT 'unverified',
        last_test_at TEXT,
        last_test_ok INTEGER,
        last_test_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_account ON integrations(user_id, provider, account_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_cache_key ON integrations(user_id, cache_account_key)",
    "CREATE INDEX IF NOT EXISTS idx_integrations_user ON integrations(user_id, enabled)",
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        value_type TEXT NOT NULL DEFAULT 'str',
        is_secret INTEGER NOT NULL DEFAULT 0,
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT,
        value_type TEXT NOT NULL DEFAULT 'str',
        updated_at TEXT,
        PRIMARY KEY (user_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_check_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        account TEXT NOT NULL,
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        sample_rate REAL NOT NULL,
        population_size INTEGER NOT NULL,
        sample_size INTEGER NOT NULL,
        judge_model TEXT,
        level_precision REAL,
        level_recall REAL,
        level_f1 REAL,
        summary_quality_avg REAL,
        summary_quality_count INTEGER,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quality_check_runs_account ON quality_check_runs(user_id, account, window_end)",
    "CREATE INDEX IF NOT EXISTS idx_quality_check_runs_window ON quality_check_runs(window_end)",
    """
    CREATE TABLE IF NOT EXISTS quality_check_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES quality_check_runs(id) ON DELETE CASCADE,
        message_id TEXT NOT NULL,
        cached_level INTEGER,
        judge_level INTEGER,
        cached_tag TEXT,
        judge_tag TEXT,
        agreement INTEGER NOT NULL,
        cached_summary TEXT,
        judge_summary TEXT,
        summary_quality_score REAL,
        judge_notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quality_check_items_run ON quality_check_items(run_id)",
)

# (table, column, ddl) added after first release. Empty for now; append here,
# never rewrite SCHEMA in place, so old DB files migrate forward safely.
LATE_COLUMNS: tuple = ()


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_APP_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn(db_path: Optional[Path] = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_app_db(db_path: Optional[Path] = None) -> Path:
    """Idempotent. Creates data/app.db (if missing) and brings its schema up to date."""
    path = db_path or DEFAULT_APP_DB_PATH
    with get_conn(path) as conn:
        cursor = conn.cursor()
        for statement in SCHEMA:
            cursor.execute(statement)
        for table, column, ddl in LATE_COLUMNS:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass
    logger.debug("App DB initialized at %s", path)
    return path
