import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import account_clients
import appdb
import integrations_store as ints
import mcp_server
import secretstore
import users_store as us
from db import EmailDB
from triage import EmailTriageEngine


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
def multi_account_user(app_db):
    """A user with two Gmail + one Zoho-OAuth integration -- the shape today's
    fixed one-Gmail+one-IMAP dashboard can't express."""
    with appdb.get_conn(app_db) as conn:
        user = us.create_user(conn, username="multi-profile", password="a_long_enough_password")
        g1 = ints.create_integration(
            conn, user_id=user["id"], provider="gmail", account_key="work@gmail.com", auth_type="oauth",
            secret={"refresh_token": "rt1"},
        )
        g2 = ints.create_integration(
            conn, user_id=user["id"], provider="gmail", account_key="personal@gmail.com", auth_type="oauth",
            secret={"refresh_token": "rt2"},
        )
        z1 = ints.create_integration(
            conn, user_id=user["id"], provider="zoho", account_key="me@zoho.com", auth_type="oauth",
            secret={"refresh_token": "rt3"}, config={"imap_host": "imap.zoho.com", "imap_port": 993},
        )
    return user, [g1, g2, z1]


def fake_settings():
    s = MagicMock()
    s.gmail_account = mcp_server._PLACEHOLDER_GMAIL_ACCOUNT
    s.imap_login = mcp_server._PLACEHOLDER_IMAP_LOGIN
    s.scheduler.max_per_account = None
    s.scheduler.days = 7
    return s


class TestSyncProfileMultiAccount:
    def test_syncs_every_connected_account(self, app_db, multi_account_user, monkeypatch):
        user, integrations = multi_account_user
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        settings = fake_settings()
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, settings))
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock())
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())

        calls = []

        def fake_sync_account(db, engine, settings_instance, client, account_label, max_results, days, stop_event=None):
            calls.append(account_label)
            return {"account": account_label, "downloaded": 1}

        monkeypatch.setattr(mcp_server, "sync_account", fake_sync_account)

        result = mcp_server.sync_profile("multi-profile")

        assert result["status"] == "ok"
        assert set(calls) == {"work@gmail.com", "personal@gmail.com", "me@zoho.com"}
        assert set(result["accounts"].keys()) == {"work@gmail.com", "personal@gmail.com", "me@zoho.com"}
        # Backward-compat keys point at the FIRST account of each family, for the pre-SPA dashboard.
        assert result["gmail"]["account"] == "work@gmail.com"
        assert result["imap"]["account"] == "me@zoho.com"

    def test_one_broken_account_does_not_abort_the_others(self, app_db, multi_account_user, monkeypatch):
        user, integrations = multi_account_user
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        settings = fake_settings()
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, settings))
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock())
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())

        def fake_sync_account(db, engine, settings_instance, client, account_label, max_results, days, stop_event=None):
            if account_label == "personal@gmail.com":
                raise RuntimeError("token revoked")
            return {"account": account_label}

        monkeypatch.setattr(mcp_server, "sync_account", fake_sync_account)

        result = mcp_server.sync_profile("multi-profile")

        assert result["accounts"]["work@gmail.com"] == {"account": "work@gmail.com"}
        assert "errors" in result["accounts"]["personal@gmail.com"]
        assert result["accounts"]["me@zoho.com"] == {"account": "me@zoho.com"}

    def test_profile_without_db_user_uses_legacy_path(self, app_db, monkeypatch):
        # No user named "not-a-db-user" exists in app.db -- must fall back exactly
        # to the original single-Gmail+single-IMAP construction.
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        settings = fake_settings()
        settings.gmail_account = "real@gmail.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, settings))
        gmail_ctor = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mcp_server, "GmailClient", gmail_ctor)
        monkeypatch.setattr(mcp_server, "sync_account", lambda *a, **k: {"account": a[4]})

        result = mcp_server.sync_profile("not-a-db-user")

        gmail_ctor.assert_called_once_with(settings_instance=settings)
        assert "accounts" not in result
        assert result["gmail"] == {"account": "real@gmail.com"}


class TestFullDownloadProfileMultiAccount:
    def test_downloads_every_connected_account(self, app_db, multi_account_user, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        settings = fake_settings()
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, MagicMock(), settings))
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock())
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())

        calls = []

        def fake_full_download(db, client, account_label, stop_event=None):
            calls.append(account_label)
            return {"account": account_label}

        monkeypatch.setattr(mcp_server, "full_download_account", fake_full_download)

        result = mcp_server.full_download_profile("multi-profile")

        assert set(calls) == {"work@gmail.com", "personal@gmail.com", "me@zoho.com"}
        assert set(result["accounts"].keys()) == {"work@gmail.com", "personal@gmail.com", "me@zoho.com"}


class TestProfileStatusMultiAccount:
    def test_status_lists_every_account(self, app_db, multi_account_user, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        fake_db.get_sync_summary.return_value = None
        fake_db.get_email_counts.return_value = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
        settings = fake_settings()
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, MagicMock(), settings))

        status = mcp_server._profile_status("multi-profile")

        assert status["configured"] is True
        assert {a["account"] for a in status["accounts"]} == {"work@gmail.com", "personal@gmail.com", "me@zoho.com"}
        assert status["gmail"]["account"] == "work@gmail.com"
        assert status["imap"]["account"] == "me@zoho.com"

    def test_status_falls_back_to_legacy_for_unknown_profile(self, app_db, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        fake_db.get_sync_summary.return_value = None
        fake_db.get_email_counts.return_value = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
        settings = fake_settings()
        settings.gmail_account = "real@gmail.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, MagicMock(), settings))

        status = mcp_server._profile_status("no-such-db-user")

        assert "accounts" not in status
        assert status["gmail"]["account"] == "real@gmail.com"
