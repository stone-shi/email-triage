import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import mcp_server
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
def admin_password():
    return "correct horse battery staple"


@pytest.fixture
def admin(app_db, admin_password):
    with appdb.get_conn(app_db) as conn:
        row = us.create_user(
            conn, username="admin", password=admin_password, is_admin=True, must_change_password=False
        )
    return row


@pytest.fixture
def client(app_db):
    from starlette.testclient import TestClient

    return TestClient(mcp_server.mcp.sse_app())


def login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestLogin:
    def test_login_success_sets_cookie(self, client, admin, admin_password):
        resp = login(client, "admin", admin_password)
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "admin"
        assert resp.json()["must_change_password"] is False
        assert "email_triage_session" in resp.cookies

    def test_login_wrong_password(self, client, admin, admin_password):
        resp = login(client, "admin", "wrong password")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_credentials"

    def test_login_unknown_user(self, client, app_db):
        resp = login(client, "nobody", "whatever")
        assert resp.status_code == 401

    def test_login_missing_fields(self, client, app_db):
        resp = client.post("/api/auth/login", json={"username": "admin"})
        assert resp.status_code == 400


class TestMeAndLogout:
    def test_me_requires_auth(self, client, app_db):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "auth_required"

    def test_me_after_login(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_logout_clears_session(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        resp2 = client.get("/api/auth/me")
        assert resp2.status_code == 401


class TestPasswordChangeGate:
    @pytest.fixture
    def forced_user(self, app_db):
        with appdb.get_conn(app_db) as conn:
            row = us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=True)
        return row

    def test_forced_user_blocked_from_active_routes(self, client, forced_user):
        login(client, "bob", "a_long_enough_password")
        resp = client.get("/api/settings")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "password_change_required"

    def test_forced_user_can_still_change_password(self, client, forced_user):
        login(client, "bob", "a_long_enough_password")
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "a_long_enough_password", "new_password": "a_new_long_enough_password"},
        )
        assert resp.status_code == 200

    def test_forced_user_can_still_call_me(self, client, forced_user):
        login(client, "bob", "a_long_enough_password")
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200

    def test_password_change_unblocks_active_routes(self, client, forced_user):
        login(client, "bob", "a_long_enough_password")
        client.post(
            "/api/auth/change-password",
            json={"current_password": "a_long_enough_password", "new_password": "a_new_long_enough_password"},
        )
        resp = client.get("/api/settings")
        assert resp.status_code == 200

    def test_change_password_wrong_current(self, client, forced_user):
        login(client, "bob", "a_long_enough_password")
        resp = client.post(
            "/api/auth/change-password", json={"current_password": "nope", "new_password": "a_new_long_password"}
        )
        assert resp.status_code == 400


class TestMcpTokens:
    @pytest.fixture
    def user(self, app_db):
        with appdb.get_conn(app_db) as conn:
            return us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)

    def test_create_list_revoke(self, client, user):
        login(client, "bob", "a_long_enough_password")
        created = client.post("/api/me/mcp-tokens", json={"label": "laptop"})
        assert created.status_code == 201
        raw_token = created.json()["token"]
        assert raw_token

        listed = client.get("/api/me/mcp-tokens")
        assert listed.status_code == 200
        assert len(listed.json()["tokens"]) == 1
        assert "token" not in listed.json()["tokens"][0]

        token_id = created.json()["id"]
        revoked = client.delete(f"/api/me/mcp-tokens/{token_id}")
        assert revoked.status_code == 200
        assert client.get("/api/me/mcp-tokens").json()["tokens"] == []


class TestAdminUsers:
    def test_non_admin_forbidden(self, client, app_db):
        with appdb.get_conn(app_db) as conn:
            us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        login(client, "bob", "a_long_enough_password")
        resp = client.get("/api/users")
        assert resp.status_code == 403

    def test_admin_creates_and_lists_users(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        created = client.post("/api/users", json={"username": "carol", "password": "a_long_enough_password"})
        assert created.status_code == 201
        assert created.json()["must_change_password"] is True

        listed = client.get("/api/users")
        assert listed.status_code == 200
        usernames = {u["username"] for u in listed.json()["items"]}
        assert {"admin", "carol"} <= usernames

    def test_admin_cannot_delete_self(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        resp = client.delete(f"/api/users/{admin['id']}")
        assert resp.status_code == 409

    def test_admin_cannot_demote_last_admin(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        resp = client.patch(f"/api/users/{admin['id']}", json={"is_admin": False})
        assert resp.status_code == 409

    def test_reset_password_forces_change(self, client, admin, admin_password, app_db):
        with appdb.get_conn(app_db) as conn:
            bob = us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        login(client, "admin", admin_password)
        resp = client.post(f"/api/users/{bob['id']}/reset-password", json={})
        assert resp.status_code == 200
        assert resp.json()["temporary_password"]
        assert resp.json()["user"]["must_change_password"] is True


class TestSettings:
    def test_get_settings_requires_login(self, client, app_db):
        resp = client.get("/api/settings")
        assert resp.status_code == 401

    def test_non_admin_can_read_but_not_write(self, client, app_db):
        with appdb.get_conn(app_db) as conn:
            us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        login(client, "bob", "a_long_enough_password")
        assert client.get("/api/settings").status_code == 200
        resp = client.put("/api/settings", json={"values": {"triage_model": "x"}})
        assert resp.status_code == 403

    def test_admin_can_write_and_secrets_are_masked_on_read(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        put_resp = client.put(
            "/api/settings", json={"values": {"triage_model": "gpt-nano", "triage_api_key": "sk-secret"}}
        )
        assert put_resp.status_code == 200

        get_resp = client.get("/api/settings")
        settings = get_resp.json()["settings"]
        assert settings["triage_model"]["value"] == "gpt-nano"
        assert settings["triage_api_key"]["value"] == "••••"
        assert "sk-secret" not in get_resp.text

    def test_unknown_key_rejected(self, client, admin, admin_password):
        login(client, "admin", admin_password)
        resp = client.put("/api/settings", json={"values": {"not_a_real_key": "x"}})
        assert resp.status_code == 400


def test_healthz_is_public(client, app_db):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
