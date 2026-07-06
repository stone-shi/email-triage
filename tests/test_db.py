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
