import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import integrations_store as ints
import secretstore
import users_store as us
from app_errors import ConflictError, NotFoundError, ValidationError


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
    # Avoid ever touching the real repo data/secret.key from a test run.
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    appdb.init_app_db(path)
    with appdb.get_conn(path) as c:
        yield c


@pytest.fixture
def user(conn):
    return us.create_user(conn, username="bob", password="a_long_enough_password")


def test_create_integration_roundtrips_secret(conn, user):
    row = ints.create_integration(
        conn,
        user_id=user["id"],
        provider="imap",
        account_key="Bob@Example.com",
        auth_type="password",
        secret={"password": "hunter2"},
    )
    assert row["account_key"] == "bob@example.com"
    assert ints.get_secret(row) == {"password": "hunter2"}


def test_create_integration_rejects_duplicate_account(conn, user):
    ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password"
    )
    with pytest.raises(ConflictError):
        ints.create_integration(
            conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password"
        )


def test_create_integration_rejects_unknown_provider(conn, user):
    with pytest.raises(ValidationError):
        ints.create_integration(
            conn, user_id=user["id"], provider="carrier-pigeon", account_key="x", auth_type="password"
        )


def test_two_providers_same_address_get_disambiguated_cache_key(conn, user):
    a = ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password"
    )
    b = ints.create_integration(
        conn, user_id=user["id"], provider="zoho", account_key="bob@example.com", auth_type="oauth"
    )
    assert a["cache_account_key"] != b["cache_account_key"]
    assert a["cache_account_key"] == "bob@example.com"


def test_require_own_rejects_other_users_row_as_not_found(conn, user):
    other = us.create_user(conn, username="alice", password="a_long_enough_password")
    row = ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password"
    )
    with pytest.raises(NotFoundError):
        ints.require_own(conn, row["id"], other["id"])


def test_upsert_oauth_integration_creates_then_updates(conn, user):
    a = ints.upsert_oauth_integration(
        conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com",
        secret={"refresh_token": "rt1"},
    )
    b = ints.upsert_oauth_integration(
        conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com",
        secret={"refresh_token": "rt2"},
    )
    assert a["id"] == b["id"]
    assert ints.get_secret(ints.get_integration(conn, a["id"])) == {"refresh_token": "rt2"}


def test_update_integration_merges_config(conn, user):
    row = ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password",
        config={"host": "imap.example.com", "port": 993},
    )
    updated = ints.update_integration(conn, row["id"], config={"port": 143})
    assert updated["config_json"]
    import json

    cfg = json.loads(updated["config_json"])
    assert cfg == {"host": "imap.example.com", "port": 143}


def test_delete_integration_removes_row(conn, user):
    row = ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password"
    )
    ints.delete_integration(conn, row["id"])
    assert ints.get_integration(conn, row["id"]) is None


def test_row_to_dict_never_leaks_raw_secret(conn, user):
    row = ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password",
        secret={"password": "hunter2"},
    )
    d = ints.row_to_dict(row)
    assert "hunter2" not in str(d)
    assert d["has_secret"] is True


def test_record_test_updates_status(conn, user):
    row = ints.create_integration(
        conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password"
    )
    ints.record_test(conn, row["id"], ok=False, error="bad password")
    updated = ints.get_integration(conn, row["id"])
    assert updated["status"] == "error"
    assert updated["last_test_error"] == "bad password"
