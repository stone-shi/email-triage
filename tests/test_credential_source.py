import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import credential_source as cs
import integrations_store as ints
import secretstore
import users_store as us


class TestFileTokenSource:
    def test_load_returns_none_when_no_token_file(self, tmp_path):
        source = cs.FileTokenSource(token_path=tmp_path / "token.json", credentials_path=tmp_path / "creds.json")
        assert source.load() is None

    def test_save_then_load_roundtrips(self, tmp_path, monkeypatch):
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        source = cs.FileTokenSource(token_path=tmp_path / "token.json", credentials_path=tmp_path / "creds.json")
        source.save(fake_creds)
        assert (tmp_path / "token.json").exists()

        loaded_creds = MagicMock()
        monkeypatch.setattr(
            cs.Credentials, "from_authorized_user_file", classmethod(lambda cls, path, scopes: loaded_creds)
        )
        assert source.load() is loaded_creds

    def test_interactive_or_fail_raises_when_no_credentials_file(self, tmp_path):
        source = cs.FileTokenSource(token_path=tmp_path / "token.json", credentials_path=tmp_path / "missing.json")
        with pytest.raises(FileNotFoundError):
            source.interactive_or_fail()


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
    appdb.init_app_db(db_path)
    return db_path


@pytest.fixture
def user(app_db):
    with appdb.get_conn(app_db) as conn:
        return us.create_user(conn, username="bob", password="a_long_enough_password")


class TestDbTokenSource:
    def test_load_returns_none_without_secret(self, app_db, user):
        with appdb.get_conn(app_db) as conn:
            row = ints.create_integration(
                conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com", auth_type="oauth"
            )
        source = cs.DbTokenSource(row["id"])
        assert source.load() is None

    def test_load_builds_credentials_from_secret(self, app_db, user):
        with appdb.get_conn(app_db) as conn:
            row = ints.create_integration(
                conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com", auth_type="oauth",
                secret={"refresh_token": "rt", "access_token": "at", "client_id": "cid", "client_secret": "csecret"},
            )
        source = cs.DbTokenSource(row["id"])
        creds = source.load()
        assert creds is not None
        assert creds.refresh_token == "rt"
        assert creds.client_id == "cid"
        assert creds.client_secret == "csecret"

    def test_save_updates_secret_and_preserves_client_id(self, app_db, user):
        with appdb.get_conn(app_db) as conn:
            row = ints.create_integration(
                conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com", auth_type="oauth",
                secret={"refresh_token": "rt1", "client_id": "cid", "client_secret": "csecret"},
            )
        source = cs.DbTokenSource(row["id"])
        fake_creds = MagicMock()
        fake_creds.token = "new-access-token"
        fake_creds.refresh_token = None  # simulates a refresh that doesn't reissue a refresh token
        fake_creds.token_uri = "https://oauth2.googleapis.com/token"
        fake_creds.client_id = None
        fake_creds.client_secret = None
        fake_creds.expiry = None
        source.save(fake_creds)

        with appdb.get_conn(app_db) as conn:
            updated = ints.get_integration(conn, row["id"])
            secret = ints.get_secret(updated)
        assert secret["access_token"] == "new-access-token"
        assert secret["refresh_token"] == "rt1"  # preserved, not clobbered by None
        assert secret["client_id"] == "cid"  # preserved

    def test_interactive_or_fail_raises_reauth_required(self):
        source = cs.DbTokenSource(integration_id=1)
        with pytest.raises(cs.ReauthRequired):
            source.interactive_or_fail()
