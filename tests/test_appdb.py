import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"


def test_init_creates_file(db_path):
    appdb.init_app_db(db_path)
    assert db_path.exists()


def test_init_creates_all_tables(db_path):
    appdb.init_app_db(db_path)
    with appdb.get_conn(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"users", "sessions", "mcp_tokens", "integrations", "app_settings", "user_settings"} <= tables


def test_init_is_idempotent(db_path):
    appdb.init_app_db(db_path)
    appdb.init_app_db(db_path)  # must not raise
    with appdb.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO users (username, workspace_slug, password_hash, password_salt, created_at, updated_at) "
            "VALUES ('a','a','h','s','now','now')"
        )
    appdb.init_app_db(db_path)
    with appdb.get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_row_factory_is_row(db_path):
    appdb.init_app_db(db_path)
    with appdb.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO users (username, workspace_slug, password_hash, password_salt, created_at, updated_at) "
            "VALUES ('a','a','h','s','now','now')"
        )
    with appdb.get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM users").fetchone()
        assert row["username"] == "a"


def test_unique_integration_account_index(db_path):
    appdb.init_app_db(db_path)
    with appdb.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO users (username, workspace_slug, password_hash, password_salt, created_at, updated_at) "
            "VALUES ('a','a','h','s','now','now')"
        )
        uid = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO integrations (user_id, provider, account_key, cache_account_key, auth_type, "
            "created_at, updated_at) VALUES (?, 'gmail', 'x@example.com', 'x@example.com', 'oauth', 'now', 'now')",
            (uid,),
        )
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO integrations (user_id, provider, account_key, cache_account_key, auth_type, "
                "created_at, updated_at) VALUES (?, 'gmail', 'x@example.com', 'other', 'oauth', 'now', 'now')",
                (uid,),
            )
