import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import mcp_tokens_store as mt
import users_store as us
from app_errors import NotFoundError


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    appdb.init_app_db(path)
    with appdb.get_conn(path) as c:
        yield c


@pytest.fixture
def user(conn):
    return us.create_user(conn, username="bob", password="a_long_enough_password")


def test_create_token_resolves(conn, user):
    raw, row = mt.create_token(conn, user["id"], label="test client")
    resolved = mt.resolve_token(conn, raw)
    assert resolved is not None
    assert resolved["id"] == row["id"]


def test_resolve_unknown_token(conn):
    assert mt.resolve_token(conn, "not-a-real-token") is None


def test_revoked_token_does_not_resolve(conn, user):
    raw, row = mt.create_token(conn, user["id"])
    mt.revoke_token(conn, user["id"], row["id"])
    assert mt.resolve_token(conn, raw) is None


def test_revoke_wrong_user_raises(conn, user):
    other = us.create_user(conn, username="alice", password="a_long_enough_password")
    raw, row = mt.create_token(conn, user["id"])
    with pytest.raises(NotFoundError):
        mt.revoke_token(conn, other["id"], row["id"])


def test_import_token_preserves_raw_value(conn, user):
    raw = "deadbeef" * 4
    row = mt.import_token(conn, user["id"], raw)
    assert mt.resolve_token(conn, raw)["id"] == row["id"]


def test_import_token_is_idempotent(conn, user):
    raw = "deadbeef" * 4
    a = mt.import_token(conn, user["id"], raw)
    b = mt.import_token(conn, user["id"], raw)
    assert a["id"] == b["id"]


def test_list_tokens_excludes_revoked_by_default(conn, user):
    raw, row = mt.create_token(conn, user["id"])
    mt.revoke_token(conn, user["id"], row["id"])
    assert mt.list_tokens(conn, user["id"]) == []
    assert len(mt.list_tokens(conn, user["id"], include_revoked=True)) == 1


def test_touch_last_used_sets_timestamp(conn, user):
    raw, row = mt.create_token(conn, user["id"])
    mt.touch_last_used(conn, row["id"], min_interval_seconds=0)
    updated = conn.execute("SELECT last_used_at FROM mcp_tokens WHERE id = ?", (row["id"],)).fetchone()
    assert updated["last_used_at"] is not None
