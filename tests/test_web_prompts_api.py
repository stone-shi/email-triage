import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import mcp_server
import prompts_store as ps
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
def admin(app_db):
    with appdb.get_conn(app_db) as conn:
        return us.create_user(
            conn, username="admin", password="a_long_enough_password", is_admin=True, must_change_password=False
        )


@pytest.fixture
def non_admin(app_db):
    with appdb.get_conn(app_db) as conn:
        return us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)


@pytest.fixture
def client(app_db):
    from starlette.testclient import TestClient

    return TestClient(mcp_server.mcp.sse_app())


def login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestListPrompts:
    def test_requires_login(self, client, app_db):
        assert client.get("/api/prompts").status_code == 401

    def test_non_admin_forbidden(self, client, non_admin):
        login(client, "bob", "a_long_enough_password")
        assert client.get("/api/prompts").status_code == 403

    def test_admin_sees_all_default_prompts(self, client, admin):
        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/prompts")
        assert resp.status_code == 200
        prompts = resp.json()["prompts"]
        assert set(prompts.keys()) == set(ps.DEFAULT_PROMPTS.keys())
        for key, entry in prompts.items():
            assert entry["value"]
            assert entry["label"]
            assert entry["source"] in ("database", "prompts.yml", "default")


class TestUpdatePrompt:
    def test_admin_can_update(self, client, admin, app_db):
        login(client, "admin", "a_long_enough_password")
        resp = client.put("/api/prompts/level_1_fast_triage", json={"value": "a brand new prompt"})
        assert resp.status_code == 200
        assert resp.json()["value"] == "a brand new prompt"
        assert resp.json()["source"] == "database"

        with appdb.get_conn(app_db) as conn:
            assert ps.get_prompt(conn, "level_1_fast_triage") == "a brand new prompt"

    def test_non_admin_forbidden(self, client, non_admin):
        login(client, "bob", "a_long_enough_password")
        resp = client.put("/api/prompts/level_1_fast_triage", json={"value": "hack"})
        assert resp.status_code == 403

    def test_unknown_key_404s(self, client, admin):
        login(client, "admin", "a_long_enough_password")
        resp = client.put("/api/prompts/not_a_real_key", json={"value": "x"})
        assert resp.status_code == 404

    def test_empty_value_rejected(self, client, admin):
        login(client, "admin", "a_long_enough_password")
        resp = client.put("/api/prompts/level_1_fast_triage", json={"value": "   "})
        assert resp.status_code == 400

    def test_updated_by_is_recorded(self, client, admin, app_db):
        login(client, "admin", "a_long_enough_password")
        client.put("/api/prompts/level_1_fast_triage", json={"value": "tracked edit"})
        with appdb.get_conn(app_db) as conn:
            row = conn.execute("SELECT updated_by FROM app_settings WHERE key = 'prompt.level_1_fast_triage'").fetchone()
            assert row["updated_by"] == admin["id"]


class TestResetPrompt:
    def test_reset_falls_back_to_default(self, client, admin, app_db):
        login(client, "admin", "a_long_enough_password")
        client.put("/api/prompts/level_1_fast_triage", json={"value": "custom"})

        resp = client.post("/api/prompts/level_1_fast_triage/reset")
        assert resp.status_code == 200
        assert resp.json()["source"] in ("default", "prompts.yml")

        with appdb.get_conn(app_db) as conn:
            assert ps.get_prompt(conn, "level_1_fast_triage") is None

    def test_non_admin_forbidden(self, client, non_admin):
        login(client, "bob", "a_long_enough_password")
        assert client.post("/api/prompts/level_1_fast_triage/reset").status_code == 403

    def test_unknown_key_404s(self, client, admin):
        login(client, "admin", "a_long_enough_password")
        assert client.post("/api/prompts/not_a_real_key/reset").status_code == 404
