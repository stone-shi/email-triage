import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import integrations_store as ints
import migrate_to_db as mig
import secretstore
import users_store as us
from config import Settings


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A synthetic profiles/ tree, isolated from the real repo -- migrate_to_db.py
    reads profile directories through its own WORKSPACE_ROOT constant, which we
    redirect here. (Settings.load_for_user/load_for_profile still resolve
    against the REAL repo root -- config.py hardcodes it -- so tests that touch
    those paths mock the classmethod directly instead of relying on this.)"""
    (tmp_path / "profiles").mkdir()
    monkeypatch.setattr(mig, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
    return db_path


def make_profile(workspace, name, *, env=None, token=None, client=None, config_yml=None):
    profile_dir = workspace / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    if env is not None:
        (profile_dir / ".env").write_text(
            "\n".join(f"{k}={v}" for k, v in env.items()), encoding="utf-8"
        )
    if token is not None:
        (profile_dir / "token.json").write_text(json.dumps(token), encoding="utf-8")
    if client is not None:
        (profile_dir / "google_cli_client.json").write_text(json.dumps(client), encoding="utf-8")
    if config_yml is not None:
        (profile_dir / "config.yml").write_text(config_yml, encoding="utf-8")
    return profile_dir


class TestRealProfileDirs:
    def test_skips_empty_directory(self, workspace):
        (workspace / "profiles" / "default").mkdir()
        assert mig._real_profile_dirs() == []

    def test_includes_directory_with_env(self, workspace):
        make_profile(workspace, "jenny", env={"EMAIL_TRIAGE_IMAP_LOGIN": "jenny@example.com"})
        assert mig._real_profile_dirs() == ["jenny"]

    def test_includes_directory_with_only_token_json(self, workspace):
        make_profile(workspace, "stone", token={"token": "x"})
        assert mig._real_profile_dirs() == ["stone"]


class TestReadEnvFile:
    def test_parses_simple_key_values(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("A=1\n# comment\nB=hello world\n", encoding="utf-8")
        assert mig._read_env_file(env_path) == {"A": "1", "B": "hello world"}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert mig._read_env_file(tmp_path / "nope.env") == {}


class TestMarker:
    def test_marker_roundtrip(self, app_db):
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            assert mig._get_marker(conn) is False
            mig._set_marker(conn)
        with appdb.get_conn(app_db) as conn:
            assert mig._get_marker(conn) is True


class TestImportGmailIntegration:
    def test_creates_integration_from_token_and_client_files(self, workspace, app_db):
        make_profile(
            workspace, "jenny",
            env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "Jenny@Gmail.com"},
            token={"token": "at", "refresh_token": "rt", "expiry": "2099-01-01T00:00:00Z", "scopes": ["a", "b"]},
            client={"installed": {"client_id": "cid", "client_secret": "csecret"}},
        )
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="jenny", password="a_long_enough_password", workspace_slug="jenny")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_gmail_integration(conn, "jenny", user["id"], report, dry_run=False)

            rows = ints.list_integrations(conn, user["id"])
            assert len(rows) == 1
            assert rows[0]["account_key"] == "jenny@gmail.com"
            secret = ints.get_secret(rows[0])
            assert secret["refresh_token"] == "rt"
            assert secret["client_id"] == "cid"
            assert report["integration_ids"][("jenny", "gmail", "jenny@gmail.com")] == rows[0]["id"]

    def test_missing_client_file_warns_but_still_imports(self, workspace, app_db):
        make_profile(
            workspace, "jenny",
            env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com"},
            token={"token": "at", "refresh_token": "rt"},
        )
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="jenny", password="a_long_enough_password", workspace_slug="jenny")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_gmail_integration(conn, "jenny", user["id"], report, dry_run=False)

            assert len(report["warnings"]) == 1
            rows = ints.list_integrations(conn, user["id"])
            assert len(rows) == 1

    def test_no_token_file_is_a_noop(self, workspace, app_db):
        make_profile(workspace, "jenny", env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com"})
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="jenny", password="a_long_enough_password", workspace_slug="jenny")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_gmail_integration(conn, "jenny", user["id"], report, dry_run=False)
            assert ints.list_integrations(conn, user["id"]) == []

    def test_placeholder_gmail_account_is_skipped_with_warning(self, workspace, app_db):
        make_profile(workspace, "jenny", token={"token": "at", "refresh_token": "rt"})
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="jenny", password="a_long_enough_password", workspace_slug="jenny")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_gmail_integration(conn, "jenny", user["id"], report, dry_run=False)
            assert ints.list_integrations(conn, user["id"]) == []
            assert report["warnings"]

    def test_dry_run_writes_nothing(self, workspace, app_db):
        make_profile(
            workspace, "jenny", env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com"},
            token={"token": "at", "refresh_token": "rt"},
        )
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="jenny", password="a_long_enough_password", workspace_slug="jenny")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_gmail_integration(conn, "jenny", user["id"], report, dry_run=True)
            assert ints.list_integrations(conn, user["id"]) == []
            assert report["integrations"] == [{"profile": "jenny", "provider": "gmail", "account_key": "jenny@gmail.com"}]


class TestImportImapIntegration:
    def test_creates_password_integration(self, workspace, app_db):
        make_profile(
            workspace, "stone",
            env={"EMAIL_TRIAGE_IMAP_LOGIN": "stone@shifamily.com", "EMAIL_TRIAGE_IMAP_PASSWORD": "hunter2"},
        )
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="stone", password="a_long_enough_password", workspace_slug="stone")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_imap_integration(conn, "stone", user["id"], report, dry_run=False)

            rows = ints.list_integrations(conn, user["id"])
            assert len(rows) == 1
            assert ints.get_secret(rows[0])["password"] == "hunter2"

    def test_no_password_is_a_noop(self, workspace, app_db):
        make_profile(workspace, "stone", env={"EMAIL_TRIAGE_IMAP_LOGIN": "stone@shifamily.com"})
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="stone", password="a_long_enough_password", workspace_slug="stone")
            report = {"warnings": [], "integrations": [], "integration_ids": {}}
            mig._import_imap_integration(conn, "stone", user["id"], report, dry_run=False)
            assert ints.list_integrations(conn, user["id"]) == []


class TestImportMcpToken:
    def test_imports_existing_token_value(self, workspace, app_db):
        make_profile(workspace, "stone", env={"EMAIL_TRIAGE_PROFILE_TOKEN": "deadbeefcafe"})
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="stone", password="a_long_enough_password", workspace_slug="stone")
            report = {"mcp_tokens": 0}
            mig._import_mcp_token(conn, "stone", user["id"], report, dry_run=False)

            import mcp_tokens_store as mt

            resolved = mt.resolve_token(conn, "deadbeefcafe")
            assert resolved is not None
            assert resolved["user_id"] == user["id"]
            assert report["mcp_tokens"] == 1

    def test_no_token_is_a_noop(self, workspace, app_db):
        make_profile(workspace, "stone", env={})
        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="stone", password="a_long_enough_password", workspace_slug="stone")
            report = {"mcp_tokens": 0}
            mig._import_mcp_token(conn, "stone", user["id"], report, dry_run=False)
            assert report["mcp_tokens"] == 0


class TestBootstrapEndToEnd:
    def test_full_import_creates_admin_and_profile_users(self, workspace, app_db, monkeypatch):
        make_profile(
            workspace, "jenny",
            env={
                "EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com",
                "EMAIL_TRIAGE_PROFILE_TOKEN": "jenny-token-123",
            },
            token={"token": "at", "refresh_token": "rt"},
            client={"installed": {"client_id": "cid", "client_secret": "csecret"}},
        )
        make_profile(
            workspace, "stone",
            env={
                "EMAIL_TRIAGE_IMAP_LOGIN": "stone@shifamily.com",
                "EMAIL_TRIAGE_IMAP_PASSWORD": "hunter2",
                "EMAIL_TRIAGE_PROFILE_TOKEN": "stone-token-456",
            },
        )
        (workspace / "profiles" / "default").mkdir()  # phantom -- must be skipped

        monkeypatch.setattr(Settings, "load_for_user", classmethod(lambda cls, user=None, **kw: Settings(_env_file=None)))
        monkeypatch.setattr(mig, "_backfill_email_cache", lambda name, report, dry_run: None)

        report = mig.bootstrap(db_path=app_db)

        usernames = {u["username"] for u in report["users"]}
        assert usernames == {"admin", "jenny", "stone"}
        admin_entry = next(u for u in report["users"] if u["username"] == "admin")
        assert admin_entry["is_admin"] is True
        stone_entry = next(u for u in report["users"] if u["username"] == "stone")
        assert stone_entry["is_admin"] is True  # DEFAULT_EXTRA_ADMIN_USERNAMES
        jenny_entry = next(u for u in report["users"] if u["username"] == "jenny")
        assert jenny_entry["is_admin"] is False

        assert len(report["integrations"]) == 2
        assert report["mcp_tokens"] == 2

        with appdb.get_conn(app_db) as conn:
            assert mig._get_marker(conn) is True
            jenny_row = us.get_user_by_username(conn, "jenny")
            assert jenny_row["must_change_password"] == 1
            assert jenny_row["workspace_slug"] == "jenny"  # unchanged -- no filesystem move

    def test_second_run_is_a_noop(self, workspace, app_db, monkeypatch):
        make_profile(workspace, "jenny", env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com"})
        monkeypatch.setattr(Settings, "load_for_user", classmethod(lambda cls, user=None, **kw: Settings(_env_file=None)))
        monkeypatch.setattr(mig, "_backfill_email_cache", lambda name, report, dry_run: None)

        first = mig.bootstrap(db_path=app_db)
        assert first["already_imported"] is False

        second = mig.bootstrap(db_path=app_db)
        assert second["already_imported"] is True
        assert second["users"] == []

    def test_dry_run_writes_no_rows_at_all(self, workspace, app_db, monkeypatch):
        make_profile(
            workspace, "jenny",
            env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com"},
            token={"token": "at", "refresh_token": "rt"},
        )
        monkeypatch.setattr(Settings, "load_for_user", classmethod(lambda cls, user=None, **kw: Settings(_env_file=None)))

        report = mig.bootstrap(db_path=app_db, dry_run=True)

        assert {u["username"] for u in report["users"]} == {"admin", "jenny"}
        with appdb.get_conn(app_db) as conn:
            assert us.count_users(conn) == 0
            assert mig._get_marker(conn) is False

    def test_idempotent_on_pre_existing_user(self, workspace, app_db, monkeypatch):
        make_profile(workspace, "jenny", env={"EMAIL_TRIAGE_GMAIL_ACCOUNT": "jenny@gmail.com"})
        monkeypatch.setattr(Settings, "load_for_user", classmethod(lambda cls, user=None, **kw: Settings(_env_file=None)))
        monkeypatch.setattr(mig, "_backfill_email_cache", lambda name, report, dry_run: None)

        appdb.init_app_db(app_db)
        with appdb.get_conn(app_db) as conn:
            us.create_user(conn, username="jenny", password="a_long_enough_password", workspace_slug="jenny")

        report = mig.bootstrap(db_path=app_db)
        jenny_entries = [u for u in report["users"] if u["username"] == "jenny"]
        assert jenny_entries == []  # already existed -- not re-created or re-reported
