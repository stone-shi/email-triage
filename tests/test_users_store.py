import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import users_store as us
from app_errors import ConflictError, ValidationError


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    appdb.init_app_db(path)
    with appdb.get_conn(path) as c:
        yield c


def test_seed_admin_creates_admin_when_empty(conn):
    created = us.seed_admin(conn)
    assert created is True
    row = us.get_user_by_username(conn, "admin")
    assert row["is_admin"] == 1
    assert row["must_change_password"] == 1


def test_seed_admin_is_idempotent(conn):
    us.seed_admin(conn)
    assert us.seed_admin(conn) is False
    assert us.count_users(conn) == 1


def test_create_user_rejects_short_password(conn):
    with pytest.raises(ValidationError):
        us.create_user(conn, username="bob", password="short")


def test_create_user_rejects_duplicate_username(conn):
    us.create_user(conn, username="bob", password="a_long_enough_password")
    with pytest.raises(ConflictError):
        us.create_user(conn, username="bob", password="another_long_password")


def test_create_user_username_case_insensitive_unique(conn):
    us.create_user(conn, username="Bob", password="a_long_enough_password")
    with pytest.raises(ConflictError):
        us.create_user(conn, username="bob", password="another_long_password")


def test_create_user_assigns_unique_workspace_slug(conn):
    a = us.create_user(conn, username="bob", password="a_long_enough_password")
    b = us.create_user(conn, username="bob!", password="another_long_password")
    assert a["workspace_slug"] != b["workspace_slug"]


def test_authenticate_success(conn):
    us.create_user(conn, username="bob", password="a_long_enough_password")
    row = us.authenticate(conn, "bob", "a_long_enough_password")
    assert row is not None
    assert row["last_login_at"] is not None


def test_authenticate_wrong_password(conn):
    us.create_user(conn, username="bob", password="a_long_enough_password")
    assert us.authenticate(conn, "bob", "wrong") is None


def test_authenticate_unknown_username(conn):
    assert us.authenticate(conn, "nobody", "whatever") is None


def test_authenticate_inactive_user_fails(conn):
    row = us.create_user(conn, username="bob", password="a_long_enough_password")
    us.update_user(conn, row["id"], is_active=False)
    assert us.authenticate(conn, "bob", "a_long_enough_password") is None


def test_update_user_cannot_demote_last_admin(conn):
    us.seed_admin(conn)
    admin = us.get_user_by_username(conn, "admin")
    with pytest.raises(ConflictError):
        us.update_user(conn, admin["id"], is_admin=False)


def test_update_user_cannot_deactivate_last_admin(conn):
    us.seed_admin(conn)
    admin = us.get_user_by_username(conn, "admin")
    with pytest.raises(ConflictError):
        us.update_user(conn, admin["id"], is_active=False)


def test_delete_user_refuses_self_delete(conn):
    row = us.create_user(conn, username="bob", password="a_long_enough_password")
    with pytest.raises(ConflictError):
        us.delete_user(conn, row["id"], requesting_user_id=row["id"])


def test_delete_user_refuses_last_admin(conn):
    us.seed_admin(conn)
    admin = us.get_user_by_username(conn, "admin")
    other = us.create_user(conn, username="bob", password="a_long_enough_password")
    with pytest.raises(ConflictError):
        us.delete_user(conn, admin["id"], requesting_user_id=other["id"])


def test_delete_user_is_soft(conn):
    us.seed_admin(conn)
    admin = us.get_user_by_username(conn, "admin")
    bob = us.create_user(conn, username="bob", password="a_long_enough_password")
    us.delete_user(conn, bob["id"], requesting_user_id=admin["id"])
    row = us.get_user(conn, bob["id"])
    assert row is not None
    assert row["is_active"] == 0


def test_session_create_and_resolve(conn):
    row = us.create_user(conn, username="bob", password="a_long_enough_password")
    token, session_id = us.create_session(conn, row["id"])
    resolved = us.resolve_session(conn, token)
    assert resolved is not None
    assert resolved[1]["id"] == row["id"]


def test_resolve_session_unknown_token(conn):
    assert us.resolve_session(conn, "not-a-real-token") is None


def test_resolve_session_expired_is_purged(conn):
    row = us.create_user(conn, username="bob", password="a_long_enough_password")
    token, session_id = us.create_session(conn, row["id"])
    conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (session_id,))
    assert us.resolve_session(conn, token) is None
    assert conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None


def test_delete_other_sessions_keeps_current(conn):
    row = us.create_user(conn, username="bob", password="a_long_enough_password")
    t1, s1 = us.create_session(conn, row["id"])
    t2, s2 = us.create_session(conn, row["id"])
    removed = us.delete_other_sessions(conn, row["id"], keep_session_id=s1)
    assert removed == 1
    assert us.resolve_session(conn, t1) is not None
    assert us.resolve_session(conn, t2) is None


def test_set_password_forces_must_change(conn):
    row = us.create_user(conn, username="bob", password="a_long_enough_password")
    us.set_password(conn, row["id"], "a_new_long_password", must_change=True)
    updated = us.authenticate(conn, "bob", "a_new_long_password")
    assert updated["must_change_password"] == 1
