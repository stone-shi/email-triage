import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import app_settings_store as ass
import oauth_flow
import oauth_google
import secretstore
import token_service


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
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


class TestClientFor:
    def test_raises_when_not_configured(self, conn):
        with pytest.raises(oauth_flow.OAuthError):
            oauth_google.client_for(conn, redirect_uri="https://app.example/cb")

    def test_builds_client_with_offline_access(self, conn):
        ass.set_value(conn, "google_client_id", "cid")
        ass.set_value(conn, "google_client_secret", "csecret")
        client = oauth_google.client_for(conn, redirect_uri="https://app.example/cb")
        assert client.client_id == "cid"
        assert ("access_type", "offline") in client.extra_authorize_params


class TestFetchIdentity:
    def test_parses_email(self, monkeypatch):
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"emailAddress": "Bob@Example.com"}
        monkeypatch.setattr(oauth_google.httpx, "get", lambda *a, **k: fake_response)

        identity = oauth_google.fetch_identity("access-token")
        assert identity["account_key"] == "bob@example.com"

    def test_raises_on_error_status(self, monkeypatch):
        fake_response = MagicMock(status_code=401, text="unauthorized")
        monkeypatch.setattr(oauth_google.httpx, "get", lambda *a, **k: fake_response)
        with pytest.raises(oauth_flow.OAuthError):
            oauth_google.fetch_identity("bad-token")

    def test_raises_when_no_email_in_response(self, monkeypatch):
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {}
        monkeypatch.setattr(oauth_google.httpx, "get", lambda *a, **k: fake_response)
        with pytest.raises(oauth_flow.OAuthError):
            oauth_google.fetch_identity("token")


class TestRefresh:
    def test_raises_reauth_required_without_refresh_token(self):
        with pytest.raises(token_service.ReauthRequired):
            oauth_google.refresh({})

    def test_raises_reauth_required_without_client_credentials(self):
        with pytest.raises(token_service.ReauthRequired):
            oauth_google.refresh({"refresh_token": "rt"})

    def test_successful_refresh_returns_new_access_token(self, monkeypatch):
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"access_token": "new-at", "expires_in": 3600}
        monkeypatch.setattr(oauth_google.httpx, "post", lambda *a, **k: fake_response)

        result = oauth_google.refresh({"refresh_token": "rt", "client_id": "cid", "client_secret": "csecret"})
        assert result["access_token"] == "new-at"
        assert "expires_at" in result
        assert "refresh_token" not in result  # not reissued by this fake response

    def test_invalid_grant_raises_reauth_required(self, monkeypatch):
        fake_response = MagicMock(status_code=400, text="invalid_grant: Token has been expired or revoked.")
        monkeypatch.setattr(oauth_google.httpx, "post", lambda *a, **k: fake_response)

        with pytest.raises(token_service.ReauthRequired):
            oauth_google.refresh({"refresh_token": "rt", "client_id": "cid", "client_secret": "csecret"})

    def test_other_error_raises_transient_token_refresh_error(self, monkeypatch):
        fake_response = MagicMock(status_code=503, text="server error")
        monkeypatch.setattr(oauth_google.httpx, "post", lambda *a, **k: fake_response)

        with pytest.raises(token_service.TokenRefreshError):
            oauth_google.refresh({"refresh_token": "rt", "client_id": "cid", "client_secret": "csecret"})
