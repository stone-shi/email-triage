import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import account_clients
import app_settings_store as ass
import appdb
import mcp_server
import oauth_flow
import oauth_google
import oauth_zoho
import secretstore
import users_store as us


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    appdb.init_app_db(db_path)
    yield db_path
    secretstore.reset_key_cache()


@pytest.fixture
def user(app_db):
    with appdb.get_conn(app_db) as conn:
        return us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)


@pytest.fixture
def client(app_db):
    from starlette.testclient import TestClient

    return TestClient(mcp_server.mcp.sse_app())


def login(client, username="bob", password="a_long_enough_password"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestProviders:
    def test_imap_always_available(self, client, user):
        login(client)
        resp = client.get("/api/integrations/providers")
        assert resp.status_code == 200
        by_id = {p["id"]: p for p in resp.json()["providers"]}
        assert by_id["imap"]["available"] is True

    def test_oauth_unavailable_without_public_base_url(self, client, user):
        login(client)
        resp = client.get("/api/integrations/providers")
        by_id = {p["id"]: p for p in resp.json()["providers"]}
        assert by_id["gmail"]["available"] is False
        assert "public base URL" in by_id["gmail"]["unavailable_reason"]

    def test_oauth_available_once_configured(self, client, user, app_db):
        with appdb.get_conn(app_db) as conn:
            ass.set_value(conn, "public_base_url", "https://triage.example.com")
            ass.set_value(conn, "google_client_id", "cid")
            ass.set_value(conn, "google_client_secret", "csecret")
        login(client)
        resp = client.get("/api/integrations/providers")
        by_id = {p["id"]: p for p in resp.json()["providers"]}
        assert by_id["gmail"]["available"] is True


class TestImapCrud:
    def test_create_list_update_delete(self, client, user):
        login(client)
        created = client.post(
            "/api/integrations",
            json={
                "provider": "imap",
                "account_key": "bob@example.com",
                "config": {"host": "imap.example.com", "port": 993},
                "secret": {"password": "hunter2"},
            },
        )
        assert created.status_code == 201
        integration_id = created.json()["id"]
        assert "hunter2" not in created.text

        listed = client.get("/api/integrations")
        assert len(listed.json()["integrations"]) == 1

        updated = client.patch(f"/api/integrations/{integration_id}", json={"account_label": "Personal"})
        assert updated.status_code == 200
        assert updated.json()["account_label"] == "Personal"

        deleted = client.delete(f"/api/integrations/{integration_id}")
        assert deleted.status_code == 200
        assert client.get("/api/integrations").json()["integrations"] == []

    def test_rejects_oauth_provider_via_direct_create(self, client, user):
        login(client)
        resp = client.post("/api/integrations", json={"provider": "gmail"})
        assert resp.status_code == 400

    def test_rejects_missing_fields(self, client, user):
        login(client)
        resp = client.post("/api/integrations", json={"provider": "imap", "account_key": "bob@example.com"})
        assert resp.status_code == 400

    def test_other_users_integration_is_404_not_403(self, client, user, app_db):
        with appdb.get_conn(app_db) as conn:
            other = us.create_user(conn, username="alice", password="a_long_enough_password", must_change_password=False)
            import integrations_store as ints

            row = ints.create_integration(
                conn, user_id=other["id"], provider="imap", account_key="alice@example.com", auth_type="password"
            )
        login(client)
        resp = client.patch(f"/api/integrations/{row['id']}", json={"account_label": "hijack"})
        assert resp.status_code == 404
        resp2 = client.delete(f"/api/integrations/{row['id']}")
        assert resp2.status_code == 404

    def test_test_route_reports_failure(self, client, user, monkeypatch):
        login(client)
        created = client.post(
            "/api/integrations",
            json={
                "provider": "imap",
                "account_key": "bob@example.com",
                "config": {"host": "imap.example.com", "port": 993},
                "secret": {"password": "hunter2"},
            },
        )
        integration_id = created.json()["id"]

        def boom(row):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(account_clients, "build_client_for_integration", boom)
        resp = client.post(f"/api/integrations/{integration_id}/test")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "connection refused" in resp.json()["error"]


class TestOAuthStart:
    def test_requires_public_base_url(self, client, user):
        login(client)
        resp = client.get("/api/integrations/oauth/gmail/start")
        assert resp.status_code == 400

    def test_returns_authorize_url_and_sets_nonce_cookie(self, client, user, app_db):
        with appdb.get_conn(app_db) as conn:
            ass.set_value(conn, "public_base_url", "https://triage.example.com")
            ass.set_value(conn, "google_client_id", "cid")
            ass.set_value(conn, "google_client_secret", "csecret")
        login(client)
        resp = client.get("/api/integrations/oauth/gmail/start")
        assert resp.status_code == 200
        assert resp.json()["authorize_url"].startswith("https://accounts.google.com/")
        assert "oauth_nonce_gmail" in resp.cookies

    def test_unknown_provider_rejected(self, client, user, app_db):
        with appdb.get_conn(app_db) as conn:
            ass.set_value(conn, "public_base_url", "https://triage.example.com")
        login(client)
        resp = client.get("/api/integrations/oauth/hotmail/start")
        assert resp.status_code == 400


class TestOAuthCallback:
    def test_missing_code_redirects_with_error(self, client, user, app_db):
        resp = client.get("/api/integrations/oauth/gmail/callback", follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_full_round_trip_creates_integration(self, client, user, app_db, monkeypatch):
        with appdb.get_conn(app_db) as conn:
            ass.set_value(conn, "public_base_url", "https://triage.example.com")
            ass.set_value(conn, "google_client_id", "cid")
            ass.set_value(conn, "google_client_secret", "csecret")
        login(client)
        start = client.get("/api/integrations/oauth/gmail/start")
        state = start.json()["authorize_url"].split("state=")[1].split("&")[0]
        from urllib.parse import unquote

        state = unquote(state)

        fake_exchange = MagicMock(return_value={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        monkeypatch.setattr(oauth_flow.OAuthClient, "exchange_code", lambda self, code: fake_exchange())
        monkeypatch.setattr(oauth_google, "fetch_identity", lambda token: {"account_key": "bob@gmail.com"})

        callback = client.get(
            f"/api/integrations/oauth/gmail/callback?code=authcode&state={state}", follow_redirects=False
        )
        assert callback.status_code == 303
        assert "connected=gmail" in callback.headers["location"]

        listed = client.get("/api/integrations")
        rows = listed.json()["integrations"]
        assert len(rows) == 1
        assert rows[0]["provider"] == "gmail"
        assert rows[0]["account_key"] == "bob@gmail.com"

    def test_tampered_state_redirects_with_error(self, client, user, app_db):
        resp = client.get(
            "/api/integrations/oauth/gmail/callback?code=x&state=tampered.state", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
