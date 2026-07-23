import sys
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import EmailDB


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.workspace_dir = Path(__file__).parent
    return s


@pytest.fixture
def db(mock_settings):
    db = EmailDB(db_path=None, settings_instance=mock_settings)
    yield db
    db_path = db.db_path
    if db_path.exists():
        db_path.unlink()


class TestEmailDBInit:
    def test_creates_db_file(self, mock_settings):
        db = EmailDB(db_path=None, settings_instance=mock_settings)
        assert db.db_path.exists()
        db.db_path.unlink()

    def test_creates_tables(self, db):
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cursor.fetchall()]
            assert "email_cache" in tables
            assert "token_logs" in tables
            assert "sync_state" in tables

    def test_email_cache_has_download_columns(self, db):
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(email_cache)")
            columns = {row[1] for row in cursor.fetchall()}
            assert "snippet" in columns
            assert "is_unread" in columns
            assert "source_id" in columns
            assert "downloaded_at" in columns


class TestEmailDBCache:
    def test_is_processed_new_message(self, db):
        assert db.is_processed("new-id-123") is False

    def test_is_processed_saved_message(self, db):
        db.save_triage_result(
            message_id="<test@example.com>",
            account="test@test.com",
            sender="alice@example.com",
            subject="Hello",
            date_str="2026-01-01",
            level_0_status="passed",
            triage_level=0,
        )
        assert db.is_processed("<test@example.com>") is True

    def test_is_processed_empty_message_id(self, db):
        assert db.is_processed("") is False
        assert db.is_processed(None) is False

    def test_is_processed_false_for_download_only_row(self, db):
        db.upsert_email_metadata(
            message_id="<downloaded-only@test.com>", account="test@test.com",
            sender="alice@example.com", subject="Hello", email_body="body text",
        )
        assert db.is_processed("<downloaded-only@test.com>") is False
        db.save_triage_result(
            message_id="<downloaded-only@test.com>",
            account="test@test.com",
            sender="alice@example.com",
            subject="Hello",
            date_str="2026-01-01",
            level_0_status="passed",
            triage_level=0,
        )
        assert db.is_processed("<downloaded-only@test.com>") is True

    def test_get_cached_result_exists(self, db):
        db.save_triage_result(
            message_id="<cached@test.com>",
            account="test@test.com",
            sender="bob@example.com",
            subject="Cached Msg",
            date_str="2026-02-15",
            level_0_status="passed",
            level_1_status="important",
            level_2_summary="This is a summary.",
            reason="Important personal email",
            score=0.95,
            triage_level=2,
            tag="personal",
            email_body="Full body text here",
            tei_enabled=True,
            tei_score=0.88,
            tei_decision="neutral",
            level_1_run=True,
            level_1_model="deepseek/flash",
            level_1_score=0.85,
            level_2_run=True,
            level_2_model="deepseek/pro",
        )
        cached = db.get_cached_result("<cached@test.com>")
        assert cached is not None
        assert cached["message_id"] == "<cached@test.com>"
        assert cached["triage_level"] == 2
        assert cached["tag"] == "personal"
        assert cached["level_2_summary"] == "This is a summary."
        assert cached["score"] == 0.95
        assert cached["tei_enabled"] == 1
        assert cached["level_1_run"] == 1

    def test_get_cached_result_not_exists(self, db):
        assert db.get_cached_result("nonexistent") is None

    def test_get_cached_result_empty(self, db):
        assert db.get_cached_result("") is None
        assert db.get_cached_result(None) is None


class TestEmailDBSaveTriageResult:
    def test_save_minimal_result(self, db):
        db.save_triage_result(
            message_id="<minimal@test.com>",
            account="test@test.com",
            sender="min@example.com",
            subject="Minimal",
            date_str="2026-03-01",
            level_0_status="filtered",
            triage_level=0,
            tag="low",
        )
        cached = db.get_cached_result("<minimal@test.com>")
        assert cached["triage_level"] == 0
        assert cached["tag"] == "low"
        assert cached["level_0_status"] == "filtered"

    def test_save_result_with_all_fields(self, db):
        db.save_triage_result(
            message_id="<full@test.com>",
            account="test@test.com",
            sender="full@example.com",
            subject="Full Test",
            date_str="2026-04-01",
            level_0_status="passed",
            level_1_status="important",
            level_2_summary="Executive summary here",
            reason="Actionable task",
            score=0.92,
            model_used_triage="deepseek/flash",
            model_used_summary="deepseek/pro",
            level_1_duration_sec=1.5,
            level_2_duration_sec=3.2,
            level_1_prompt_tokens=120,
            level_1_completion_tokens=40,
            level_2_prompt_tokens=500,
            level_2_completion_tokens=180,
            triage_level=2,
            tag="vip",
            email_body="Full email body content",
            tei_enabled=False,
            tei_score=None,
            tei_decision=None,
            level_1_run=True,
            level_1_model="deepseek/flash",
            level_1_score=0.85,
            level_2_run=True,
            level_2_model="deepseek/pro",
        )
        cached = db.get_cached_result("<full@test.com>")
        assert cached["message_id"] == "<full@test.com>"
        assert cached["triage_level"] == 2
        assert cached["level_1_duration_sec"] == 1.5
        assert cached["level_2_duration_sec"] == 3.2
        assert cached["level_1_prompt_tokens"] == 120
        assert cached["tei_enabled"] == 0
        assert cached["level_2_run"] == 1

    def test_save_replaces_existing(self, db):
        db.save_triage_result(
            message_id="<replace@test.com>",
            account="test@test.com",
            sender="first@example.com",
            subject="First",
            date_str="2026-01-01",
            level_0_status="passed",
            triage_level=1,
            tag="notification",
        )
        db.save_triage_result(
            message_id="<replace@test.com>",
            account="test@test.com",
            sender="second@example.com",
            subject="Second",
            date_str="2026-02-02",
            level_0_status="passed",
            triage_level=2,
            tag="vip",
        )
        cached = db.get_cached_result("<replace@test.com>")
        assert cached["triage_level"] == 2
        assert cached["subject"] == "Second"


class TestEmailDBTokenLogs:
    def test_log_token_usage(self, db):
        db.log_token_usage("level_1_classification", "deepseek/flash", 150)
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT event, model, tokens_used FROM token_logs")
            rows = cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "level_1_classification"
            assert rows[0][1] == "deepseek/flash"
            assert rows[0][2] == 150


class TestEmailDBSyncState:
    def test_get_sync_checkpoint_none(self, db):
        assert db.get_sync_checkpoint("gmail") is None

    def test_save_and_get_checkpoint(self, db):
        db.save_sync_checkpoint("gmail", "1234567890")
        assert db.get_sync_checkpoint("gmail") == "1234567890"

    def test_save_overwrites_checkpoint(self, db):
        db.save_sync_checkpoint("gmail", "v1")
        db.save_sync_checkpoint("gmail", "v2")
        assert db.get_sync_checkpoint("gmail") == "v2"


class TestEmailDBDailyImportant:
    def test_get_daily_important_emails(self, db):
        db.save_triage_result(
            message_id="<imp1@test.com>",
            account="test@test.com",
            sender="vip@example.com",
            subject="Urgent Task",
            date_str="2026-07-06",
            level_0_status="passed",
            level_1_status="important",
            level_2_summary="Need action by EOD",
            triage_level=2,
            tag="vip",
        )
        db.save_triage_result(
            message_id="<noise@test.com>",
            account="test@test.com",
            sender="spam@example.com",
            subject="Buy now!",
            date_str="2026-07-06",
            level_0_status="filtered",
            level_1_status="skipped",
            triage_level=0,
            tag="low",
        )
        important = db.get_daily_important_emails()
        assert len(important) == 1
        assert important[0]["sender"] == "vip@example.com"
        assert important[0]["level_2_summary"] == "Need action by EOD"


class TestEmailDBUpsertMetadata:
    def test_upsert_creates_pending_row(self, db):
        db.upsert_email_metadata(
            message_id="<pending@test.com>", account="acct@test.com", sender="a@b.com",
            subject="Subj", date_str="2026-01-01", snippet="snip", source_id="123",
            email_body="body", is_unread=True,
        )
        cached = db.get_cached_result("<pending@test.com>")
        assert cached is not None
        assert cached["triage_level"] is None
        assert cached["email_body"] == "body"
        assert cached["is_unread"] == 1
        assert cached["source_id"] == "123"

    def test_upsert_preserves_body_when_not_supplied(self, db):
        db.upsert_email_metadata(
            message_id="<preserve@test.com>", account="acct@test.com", email_body="original body",
        )
        db.upsert_email_metadata(message_id="<preserve@test.com>", account="acct@test.com", is_unread=False)
        cached = db.get_cached_result("<preserve@test.com>")
        assert cached["email_body"] == "original body"
        assert cached["is_unread"] == 0

    def test_upsert_toggle_unread_preserves_other_fields(self, db):
        db.upsert_email_metadata(
            message_id="<toggle@test.com>", account="acct@test.com", sender="s@x.com",
            subject="Toggle", is_unread=True,
        )
        db.upsert_email_metadata(message_id="<toggle@test.com>", account="acct@test.com", is_unread=False)
        cached = db.get_cached_result("<toggle@test.com>")
        assert cached["is_unread"] == 0
        assert cached["sender"] == "s@x.com"
        assert cached["subject"] == "Toggle"


class TestEmailDBSaveTriageResultCoalesce:
    def test_save_triage_result_preserves_downloaded_body(self, db):
        db.upsert_email_metadata(
            message_id="<coalesce@test.com>", account="acct@test.com", email_body="downloaded body",
        )
        db.save_triage_result(
            message_id="<coalesce@test.com>",
            account="acct@test.com",
            sender="s@x.com",
            subject="Subj",
            date_str="2026-01-01",
            level_0_status="filtered",
            triage_level=0,
            tag="low",
            email_body=None,
        )
        cached = db.get_cached_result("<coalesce@test.com>")
        assert cached["email_body"] == "downloaded body"
        assert cached["triage_level"] == 0


class TestEmailDBUnreadQueries:
    def test_get_unread_emails_filters_by_flag_and_account(self, db):
        db.upsert_email_metadata(message_id="<u1@test.com>", account="acct-a@test.com", is_unread=True)
        db.upsert_email_metadata(message_id="<u2@test.com>", account="acct-a@test.com", is_unread=False)
        db.upsert_email_metadata(message_id="<u3@test.com>", account="acct-b@test.com", is_unread=True)

        all_unread = db.get_unread_emails()
        assert {r["message_id"] for r in all_unread} == {"<u1@test.com>", "<u3@test.com>"}

        acct_a_unread = db.get_unread_emails(account="acct-a@test.com")
        assert [r["message_id"] for r in acct_a_unread] == ["<u1@test.com>"]

    def test_get_unread_message_ids(self, db):
        db.upsert_email_metadata(message_id="<v1@test.com>", account="acct@test.com", is_unread=True)
        db.upsert_email_metadata(message_id="<v2@test.com>", account="acct@test.com", is_unread=False)
        ids = db.get_unread_message_ids("acct@test.com")
        assert ids == {"<v1@test.com>"}


class TestEmailDBSyncSummary:
    def test_get_sync_summary_missing_returns_none(self, db):
        assert db.get_sync_summary("acct@test.com") is None

    def test_save_and_get_sync_summary_roundtrip(self, db):
        summary = {"downloaded": 5, "triaged": 2, "errors": [], "last_download_at": "2026-07-23T00:00:00+00:00"}
        db.save_sync_summary("acct@test.com", summary)
        assert db.get_sync_summary("acct@test.com") == summary
