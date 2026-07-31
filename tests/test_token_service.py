import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import integrations_store as ints
import secretstore
import token_service
import users_store as us


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    token_service._locks.clear()
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


def make_integration(app_db, user, *, expires_in=None, secret=None):
    with appdb.get_conn(app_db) as conn:
        row = ints.create_integration(
            conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com", auth_type="oauth",
            secret=secret or {"access_token": "old-token", "refresh_token": "rt"},
        )
        if expires_in is not None:
            expires_at = (datetime.now(timezone.utc) + expires_in).isoformat()
            conn.execute("UPDATE integrations SET token_expires_at = ? WHERE id = ?", (expires_at, row["id"]))
        return ints.get_integration(conn, row["id"])


class TestAccessToken:
    def test_returns_cached_token_when_still_fresh(self, app_db, user, monkeypatch):
        row = make_integration(app_db, user, expires_in=timedelta(hours=1))
        refresh = MagicMock()
        monkeypatch.setattr(token_service, "_provider_refresh", refresh)

        token = token_service.access_token(row["id"])

        assert token == "old-token"
        refresh.assert_not_called()

    def test_refreshes_when_expired(self, app_db, user, monkeypatch):
        row = make_integration(app_db, user, expires_in=timedelta(seconds=-10))
        monkeypatch.setattr(
            token_service, "_provider_refresh",
            lambda provider, secret: {"access_token": "new-token", "expires_at": "2099-01-01T00:00:00+00:00"},
        )

        token = token_service.access_token(row["id"])

        assert token == "new-token"
        with appdb.get_conn(app_db) as conn:
            updated = ints.get_integration(conn, row["id"])
            assert ints.get_secret(updated)["access_token"] == "new-token"
            assert ints.get_secret(updated)["refresh_token"] == "rt"  # preserved
            assert updated["secret_version"] == row["secret_version"] + 1

    def test_refreshes_when_no_expiry_recorded(self, app_db, user, monkeypatch):
        row = make_integration(app_db, user, expires_in=None)
        monkeypatch.setattr(
            token_service, "_provider_refresh",
            lambda provider, secret: {"access_token": "new-token", "expires_at": "2099-01-01T00:00:00+00:00"},
        )
        assert token_service.access_token(row["id"]) == "new-token"

    def test_reauth_required_marks_integration(self, app_db, user, monkeypatch):
        row = make_integration(app_db, user, expires_in=timedelta(seconds=-10))

        def raise_reauth(provider, secret):
            raise token_service.ReauthRequired("refresh token revoked")

        monkeypatch.setattr(token_service, "_provider_refresh", raise_reauth)

        with pytest.raises(token_service.ReauthRequired):
            token_service.access_token(row["id"])

        with appdb.get_conn(app_db) as conn:
            updated = ints.get_integration(conn, row["id"])
            assert updated["status"] == "reauth_required"

    def test_unknown_integration_raises(self, app_db):
        with pytest.raises(token_service.TokenRefreshError):
            token_service.access_token(999999)

    def test_lost_cas_reads_back_other_writers_value(self, app_db, user, monkeypatch):
        row = make_integration(app_db, user, expires_in=timedelta(seconds=-10))

        # Simulate a concurrent writer bumping secret_version between our read and our UPDATE.
        real_refresh = lambda provider, secret: {"access_token": "mine", "expires_at": "2099-01-01T00:00:00+00:00"}

        def sneaky_refresh(provider, secret):
            with appdb.get_conn(app_db) as conn:
                conn.execute(
                    "UPDATE integrations SET secret_json = ?, secret_version = secret_version + 1 WHERE id = ?",
                    (secretstore.encrypt({"access_token": "theirs", "refresh_token": "rt"}), row["id"]),
                )
            return real_refresh(provider, secret)

        monkeypatch.setattr(token_service, "_provider_refresh", sneaky_refresh)

        token = token_service.access_token(row["id"])
        assert token == "theirs"
