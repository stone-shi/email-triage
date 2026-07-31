import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import account_clients
import appdb
import gmail_client
import imap_client
import integrations_store as ints
import secretstore
import users_store as us
from triage import EmailTriageEngine


def make_engine(settings, db):
    eng = EmailTriageEngine.__new__(EmailTriageEngine)
    eng.settings = settings
    eng.db = db
    return eng


def make_unread(mid, internal_id):
    return {"message_id": mid, "id": internal_id}


@pytest.fixture
def legacy_settings():
    settings = MagicMock()
    settings.workspace_dir = Path("/tmp/not-a-real-profile-dir")
    settings.gmail_account = "gmail@test.com"
    settings.imap_login = "imap@test.com"
    return settings


class TestLegacyPath:
    """No data/app.db exists in the test environment, so mark_emails_read always
    takes the original single-Gmail+single-IMAP path here -- these tests patch
    GmailClient/IMAPClient in their OWN defining modules, since triage.py's
    `from gmail_client import GmailClient` re-resolves that lookup on every call."""

    def test_marks_all_emails_across_both_accounts(self, legacy_settings, monkeypatch):
        db = MagicMock()
        db.get_cached_result.return_value = None
        eng = make_engine(legacy_settings, db)

        fake_gmail = MagicMock()
        fake_gmail.fetch_unread_messages.return_value = [make_unread("<g1>", "g1")]
        fake_gmail.mark_as_read.return_value = True
        fake_imap = MagicMock()
        fake_imap.fetch_unread_headers.return_value = [make_unread("<i1>", "i1")]
        fake_imap.mark_as_read.return_value = True

        monkeypatch.setattr(gmail_client, "GmailClient", lambda settings_instance: fake_gmail)
        monkeypatch.setattr(imap_client, "IMAPClient", lambda settings_instance: fake_imap)

        result = eng.mark_emails_read(all_emails=True)

        assert result["gmail_marked_count"] == 1
        assert result["imap_marked_count"] == 1
        assert result["gmail_ids"] == ["g1"]
        assert result["imap_uids"] == ["i1"]
        assert result["errors"] == []
        assert {a["provider"] for a in result["accounts"]} == {"gmail", "imap"}

    def test_gmail_construction_failure_is_recorded_and_imap_still_runs(self, legacy_settings, monkeypatch):
        db = MagicMock()
        db.get_cached_result.return_value = None
        eng = make_engine(legacy_settings, db)

        def boom_gmail(settings_instance):
            raise RuntimeError("gmail down")

        fake_imap = MagicMock()
        fake_imap.fetch_unread_headers.return_value = []

        monkeypatch.setattr(gmail_client, "GmailClient", boom_gmail)
        monkeypatch.setattr(imap_client, "IMAPClient", lambda settings_instance: fake_imap)

        result = eng.mark_emails_read(all_emails=True)

        assert result["gmail_marked_count"] == 0
        assert any("gmail" in e.lower() for e in result["errors"])
        assert {a["provider"] for a in result["accounts"]} == {"gmail", "imap"}

    def test_message_id_filter_matches_by_rfc_id_or_internal_id(self, legacy_settings, monkeypatch):
        db = MagicMock()
        db.get_cached_result.return_value = None
        eng = make_engine(legacy_settings, db)

        fake_gmail = MagicMock()
        fake_gmail.fetch_unread_messages.return_value = [make_unread("<g1>", "g1"), make_unread("<g2>", "g2")]
        fake_gmail.mark_as_read.return_value = True
        fake_imap = MagicMock()
        fake_imap.fetch_unread_headers.return_value = []

        monkeypatch.setattr(gmail_client, "GmailClient", lambda settings_instance: fake_gmail)
        monkeypatch.setattr(imap_client, "IMAPClient", lambda settings_instance: fake_imap)

        result = eng.mark_emails_read(message_id="<g2>")

        assert result["gmail_ids"] == ["g2"]
        fake_gmail.mark_as_read.assert_called_once_with(["g2"])

    def test_level_filter_uses_cached_triage_level(self, legacy_settings, monkeypatch):
        db = MagicMock()
        db.get_cached_result.side_effect = lambda mid: {"triage_level": 2} if mid == "<g2>" else {"triage_level": 0}
        eng = make_engine(legacy_settings, db)

        fake_gmail = MagicMock()
        fake_gmail.fetch_unread_messages.return_value = [make_unread("<g1>", "g1"), make_unread("<g2>", "g2")]
        fake_gmail.mark_as_read.return_value = True
        fake_imap = MagicMock()
        fake_imap.fetch_unread_headers.return_value = []

        monkeypatch.setattr(gmail_client, "GmailClient", lambda settings_instance: fake_gmail)
        monkeypatch.setattr(imap_client, "IMAPClient", lambda settings_instance: fake_imap)

        result = eng.mark_emails_read(level=2)

        assert result["gmail_ids"] == ["g2"]

    def test_updates_local_cache_for_marked_messages(self, legacy_settings, monkeypatch):
        db = MagicMock()
        db.get_cached_result.return_value = None
        eng = make_engine(legacy_settings, db)

        fake_gmail = MagicMock()
        fake_gmail.fetch_unread_messages.return_value = [make_unread("<g1>", "g1")]
        fake_gmail.mark_as_read.return_value = True
        fake_imap = MagicMock()
        fake_imap.fetch_unread_headers.return_value = []

        monkeypatch.setattr(gmail_client, "GmailClient", lambda settings_instance: fake_gmail)
        monkeypatch.setattr(imap_client, "IMAPClient", lambda settings_instance: fake_imap)

        eng.mark_emails_read(all_emails=True)

        db.upsert_email_metadata.assert_called_once_with(message_id="<g1>", account="gmail@test.com", is_unread=False)


class TestDbBackedPath:
    @pytest.fixture(autouse=True)
    def _isolated_secret_key(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
        secretstore.reset_key_cache()
        yield
        secretstore.reset_key_cache()

    @pytest.fixture
    def app_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "app.db"
        monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
        appdb.init_app_db(db_path)
        return db_path

    def test_loops_over_every_connected_account(self, app_db, monkeypatch):
        with appdb.get_conn(app_db) as conn:
            user = us.create_user(conn, username="multi-mark", password="a_long_enough_password")
            ints.create_integration(
                conn, user_id=user["id"], provider="gmail", account_key="a@gmail.com", auth_type="oauth",
                secret={"refresh_token": "rt1"},
            )
            ints.create_integration(
                conn, user_id=user["id"], provider="gmail", account_key="b@gmail.com", auth_type="oauth",
                secret={"refresh_token": "rt2"},
            )

        fake_client_a = MagicMock()
        fake_client_a.fetch_unread_messages.return_value = [make_unread("<a1>", "a1")]
        fake_client_a.mark_as_read.return_value = True
        fake_client_b = MagicMock()
        fake_client_b.fetch_unread_messages.return_value = [make_unread("<b1>", "b1")]
        fake_client_b.mark_as_read.return_value = True
        clients_by_account = {"a@gmail.com": fake_client_a, "b@gmail.com": fake_client_b}

        def fake_build_gmail(row):
            return account_clients.AccountClient(
                integration_id=row["id"], provider="gmail", account=row["cache_account_key"],
                label=row["cache_account_key"], client=clients_by_account[row["cache_account_key"]],
            )

        monkeypatch.setattr(account_clients, "_build_gmail", fake_build_gmail)

        settings = MagicMock()
        settings.workspace_dir = Path("/tmp") / "multi-mark"
        db = MagicMock()
        db.get_cached_result.return_value = None
        eng = make_engine(settings, db)

        result = eng.mark_emails_read(all_emails=True)

        assert {a["account"] for a in result["accounts"]} == {"a@gmail.com", "b@gmail.com"}
        assert sum(a["marked"] for a in result["accounts"]) == 2

    def test_falls_back_to_legacy_when_no_db_user_matches(self, app_db, monkeypatch):
        settings = MagicMock()
        settings.workspace_dir = Path("/tmp") / "no-such-user"
        settings.gmail_account = "gmail@test.com"
        settings.imap_login = "imap@test.com"
        db = MagicMock()
        db.get_cached_result.return_value = None
        eng = make_engine(settings, db)

        fake_gmail = MagicMock()
        fake_gmail.fetch_unread_messages.return_value = []
        fake_imap = MagicMock()
        fake_imap.fetch_unread_headers.return_value = []
        monkeypatch.setattr(gmail_client, "GmailClient", lambda settings_instance: fake_gmail)
        monkeypatch.setattr(imap_client, "IMAPClient", lambda settings_instance: fake_imap)

        result = eng.mark_emails_read(all_emails=True)

        assert {a["provider"] for a in result["accounts"]} == {"gmail", "imap"}
