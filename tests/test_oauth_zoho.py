import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import app_settings_store as ass
import oauth_flow
import oauth_zoho
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


class TestDcResolution:
    def test_accounts_host(self):
        assert oauth_zoho.accounts_host("com") == "https://accounts.zoho.com"
        assert oauth_zoho.accounts_host("eu") == "https://accounts.zoho.eu"

    def test_dc_from_accounts_server(self):
        assert oauth_zoho.dc_from_accounts_server("https://accounts.zoho.eu") == "eu"
        assert oauth_zoho.dc_from_accounts_server("https://accounts.zoho.com/") == "com"
        assert oauth_zoho.dc_from_accounts_server(None) is None
        assert oauth_zoho.dc_from_accounts_server("https://unrelated.example") is None

    def test_resolve_dc_defaults_when_unset(self, conn):
        assert oauth_zoho.resolve_dc(conn) == "com"

    def test_resolve_dc_uses_configured_value(self, conn):
        ass.set_value(conn, "zoho_dc", "eu")
        assert oauth_zoho.resolve_dc(conn) == "eu"


class TestClientFor:
    def test_raises_when_not_configured(self, conn):
        with pytest.raises(oauth_flow.OAuthError):
            oauth_zoho.client_for(conn, redirect_uri="https://app.example/cb")

    def test_builds_client_using_resolved_dc(self, conn):
        ass.set_value(conn, "zoho_client_id", "cid")
        ass.set_value(conn, "zoho_client_secret", "csecret")
        ass.set_value(conn, "zoho_dc", "eu")
        client = oauth_zoho.client_for(conn, redirect_uri="https://app.example/cb")
        assert client.authorize_url == "https://accounts.zoho.eu/oauth/v2/auth"
        assert ("access_type", "offline") in client.extra_authorize_params


class TestFetchIdentity:
    def test_uses_zoho_oauthtoken_header_not_bearer(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"Email": "Bob@Example.com", "ZUID": "123"}
            return resp

        monkeypatch.setattr(oauth_zoho.httpx, "get", fake_get)
        identity = oauth_zoho.fetch_identity("access-token", dc="com")

        assert captured["headers"]["Authorization"] == "Zoho-oauthtoken access-token"
        assert identity["account_key"] == "bob@example.com"
        assert identity["zuid"] == "123"

    def test_raises_when_no_email(self, monkeypatch):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {}
        monkeypatch.setattr(oauth_zoho.httpx, "get", lambda *a, **k: resp)
        with pytest.raises(oauth_flow.OAuthError):
            oauth_zoho.fetch_identity("token")


class TestRefresh:
    def test_raises_reauth_required_without_refresh_token(self):
        with pytest.raises(token_service.ReauthRequired):
            oauth_zoho.refresh({})

    def test_successful_refresh_preserves_refresh_token_when_not_reissued(self, monkeypatch):
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"access_token": "new-at", "expires_in": 3600}
        monkeypatch.setattr(oauth_zoho.httpx, "post", lambda *a, **k: fake_response)

        result = oauth_zoho.refresh(
            {"refresh_token": "rt", "client_id": "cid", "client_secret": "csecret", "dc": "com"}
        )
        assert result["access_token"] == "new-at"
        assert "refresh_token" not in result

    def test_invalid_response_raises_reauth_required(self, monkeypatch):
        fake_response = MagicMock(status_code=400, text="invalid_code: token invalid")
        monkeypatch.setattr(oauth_zoho.httpx, "post", lambda *a, **k: fake_response)

        with pytest.raises(token_service.ReauthRequired):
            oauth_zoho.refresh({"refresh_token": "rt", "client_id": "cid", "client_secret": "csecret"})

    def test_server_error_raises_transient_error(self, monkeypatch):
        fake_response = MagicMock(status_code=503, text="server error")
        monkeypatch.setattr(oauth_zoho.httpx, "post", lambda *a, **k: fake_response)

        with pytest.raises(token_service.TokenRefreshError):
            oauth_zoho.refresh({"refresh_token": "rt", "client_id": "cid", "client_secret": "csecret"})
