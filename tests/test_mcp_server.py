import sys
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mcp_server
from db import EmailDB
from triage import EmailTriageEngine
from gmail_client import GmailClient
from imap_client import IMAPClient


def make_fake_settings(**overrides):
    s = MagicMock()
    s.gmail_account = "gmail@test.com"
    s.imap_host = "imap.test.com"
    s.imap_port = 993
    s.imap_login = "imap@test.com"
    s.imap_password = ""
    s.smtp_host = "smtp.test.com"
    s.smtp_port = 465
    s.active_smtp_login = "gmail@test.com"
    s.active_smtp_password = ""
    s.triage_base_url = "http://triage.test"
    s.triage_model = "model-a"
    s.triage_api_key = ""
    s.summary_base_url = "http://summary.test"
    s.summary_model = "model-b"
    s.summary_api_key = ""
    s.triage.confidence_threshold = 0.8
    s.triage.triage_type = "llm"
    s.triage.tei_url = "http://tei.test"
    s.triage.tei_model = "tei-model"
    s.triage.tei_api_key = ""
    s.triage.tei_router_enabled = False
    s.triage.tei_noise_enabled = True
    s.triage.tei_signal_enabled = True
    s.triage.tei_noise_threshold = 0.9
    s.triage.tei_signal_threshold = 0.9
    s.triage.whitelist_vip_senders = []
    s.triage.whitelist_domains = []
    s.triage.blacklist_keywords = []
    s.triage.blacklist_senders = []
    s.scheduler.enabled = True
    s.scheduler.interval = "15m"
    s.scheduler.max_per_account = None
    s.scheduler.days = None
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def make_email(msg_id, sender="s@x.com", subject="Subj", date="2026-01-01", snippet="snip", eid="internal-1"):
    return {
        "id": eid, "message_id": msg_id, "sender": sender, "subject": subject,
        "date": date, "snippet": snippet, "account": "acct@test.com",
    }


class TestSyncAccount:
    def test_downloads_and_triages_new_unread(self):
        db = MagicMock(spec=EmailDB)
        db.get_unread_message_ids.return_value = set()
        db.get_cached_result.return_value = None
        engine = MagicMock(spec=EmailTriageEngine)
        engine.is_vip_sender.return_value = False
        engine.run_level_0_static.return_value = (True, "noise")

        client = MagicMock(spec=GmailClient)
        email = make_email("<a@test.com>")
        client.fetch_unread_messages.return_value = [email]
        client.fetch_full_body.return_value = "full body text"

        settings = MagicMock()
        settings.triage.confidence_threshold = 0.8

        summary = mcp_server.sync_account(db, engine, settings, client, "acct@test.com", None, 7)

        assert summary["downloaded"] == 1
        assert summary["triaged"] == 1
        assert summary["reconciled_read"] == 0
        db.upsert_email_metadata.assert_any_call(
            message_id="<a@test.com>", account="acct@test.com", sender="s@x.com", subject="Subj",
            date_str="2026-01-01", snippet="snip", source_id="internal-1", email_body="full body text",
            is_unread=True,
        )
        db.save_triage_result.assert_called_once()
        db.save_sync_summary.assert_called_once()

    def test_reconciles_no_longer_unread_messages(self):
        db = MagicMock(spec=EmailDB)
        db.get_unread_message_ids.return_value = {"<old@test.com>"}
        db.get_cached_result.return_value = {"triage_level": 0, "email_body": "cached body"}
        engine = MagicMock(spec=EmailTriageEngine)
        client = MagicMock(spec=GmailClient)
        client.fetch_unread_messages.return_value = []

        settings = MagicMock()
        settings.triage.confidence_threshold = 0.8

        summary = mcp_server.sync_account(db, engine, settings, client, "acct@test.com", None, 7)

        assert summary["reconciled_read"] == 1
        db.upsert_email_metadata.assert_any_call(message_id="<old@test.com>", account="acct@test.com", is_unread=False)

    def test_skips_retriage_when_already_triaged(self):
        db = MagicMock(spec=EmailDB)
        db.get_unread_message_ids.return_value = set()
        db.get_cached_result.return_value = {"triage_level": 2, "email_body": "already have body"}
        engine = MagicMock(spec=EmailTriageEngine)
        client = MagicMock(spec=GmailClient)
        email = make_email("<b@test.com>")
        client.fetch_unread_messages.return_value = [email]

        settings = MagicMock()
        settings.triage.confidence_threshold = 0.8

        summary = mcp_server.sync_account(db, engine, settings, client, "acct@test.com", None, 7)

        assert summary["downloaded"] == 1
        assert summary["triaged"] == 0
        client.fetch_full_body.assert_not_called()
        engine.is_vip_sender.assert_not_called()

    def test_progress_cleared_after_completion(self):
        db = MagicMock(spec=EmailDB)
        db.get_unread_message_ids.return_value = set()
        db.get_cached_result.return_value = None
        engine = MagicMock(spec=EmailTriageEngine)
        engine.is_vip_sender.return_value = False
        engine.run_level_0_static.return_value = (True, "noise")
        client = MagicMock(spec=GmailClient)
        client.fetch_unread_messages.return_value = [make_email("<c@test.com>")]
        client.fetch_full_body.return_value = "body"
        settings = MagicMock()
        settings.triage.confidence_threshold = 0.8

        mcp_server.sync_account(db, engine, settings, client, "progress-acct@test.com", None, 7)

        assert mcp_server._get_progress("progress-acct@test.com") is None

    def test_progress_cleared_even_on_error(self):
        db = MagicMock(spec=EmailDB)
        db.get_unread_message_ids.side_effect = RuntimeError("boom")
        engine = MagicMock(spec=EmailTriageEngine)
        client = MagicMock(spec=GmailClient)
        client.fetch_unread_messages.return_value = []
        settings = MagicMock()

        summary = mcp_server.sync_account(db, engine, settings, client, "error-acct@test.com", None, 7)

        assert summary["errors"]
        assert mcp_server._get_progress("error-acct@test.com") is None


class TestSyncProgressHelpers:
    def test_set_get_clear_roundtrip(self):
        account = "progress-helper-acct@test.com"
        assert mcp_server._get_progress(account) is None

        mcp_server._set_progress(account, phase="syncing", total=10, processed=0)
        assert mcp_server._get_progress(account) == {"phase": "syncing", "total": 10, "processed": 0}

        mcp_server._set_progress(account, processed=3)
        assert mcp_server._get_progress(account)["processed"] == 3

        mcp_server._clear_progress(account)
        assert mcp_server._get_progress(account) is None

    def test_returned_snapshot_is_a_copy(self):
        account = "progress-copy-acct@test.com"
        mcp_server._set_progress(account, total=5, processed=1)
        snapshot = mcp_server._get_progress(account)
        snapshot["processed"] = 999

        assert mcp_server._get_progress(account)["processed"] == 1
        mcp_server._clear_progress(account)


class TestSyncProfile:
    def test_merges_gmail_and_imap_results(self, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        fake_settings = MagicMock()
        fake_settings.gmail_account = "gmail@test.com"
        fake_settings.imap_login = "imap@test.com"
        fake_settings.scheduler.max_per_account = None
        fake_settings.scheduler.days = 7

        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, fake_settings))
        monkeypatch.setattr(mcp_server, "GmailClient", lambda settings_instance: MagicMock(spec=GmailClient))
        monkeypatch.setattr(mcp_server, "IMAPClient", lambda settings_instance: MagicMock(spec=IMAPClient))

        calls = []

        def fake_sync_account(db, engine, settings, client, account_label, max_results, days, stop_event=None):
            calls.append(account_label)
            return {"account": account_label}

        monkeypatch.setattr(mcp_server, "sync_account", fake_sync_account)

        result = mcp_server.sync_profile("merge-test-profile")

        assert result["status"] == "ok"
        assert result["gmail"] == {"account": "gmail@test.com"}
        assert result["imap"] == {"account": "imap@test.com"}
        assert set(calls) == {"gmail@test.com", "imap@test.com"}

    def test_concurrent_calls_skip_second(self, monkeypatch):
        release = threading.Event()
        entered = threading.Event()

        def slow_get_resources(profile):
            entered.set()
            release.wait(timeout=2)
            return (MagicMock(spec=EmailDB), MagicMock(spec=EmailTriageEngine), MagicMock())

        monkeypatch.setattr(mcp_server, "get_resources", slow_get_resources)
        monkeypatch.setattr(mcp_server, "GmailClient", lambda settings_instance: MagicMock(spec=GmailClient))
        monkeypatch.setattr(mcp_server, "IMAPClient", lambda settings_instance: MagicMock(spec=IMAPClient))
        monkeypatch.setattr(mcp_server, "sync_account", lambda *a, **k: {"ok": True})

        results = []

        def run():
            results.append(mcp_server.sync_profile("lock-test-profile"))

        t1 = threading.Thread(target=run)
        t1.start()
        entered.wait(timeout=2)
        second_result = mcp_server.sync_profile("lock-test-profile")
        release.set()
        t1.join(timeout=2)

        assert second_result["status"] == "skipped"
        assert results[0]["status"] == "ok"


class TestFetchAndProcessUnreadCacheOnly:
    def test_no_live_client_construction(self, monkeypatch):
        db = MagicMock(spec=EmailDB)
        rows_gmail = [{
            "message_id": "<g1@test.com>", "sender": "s@x.com", "subject": "Subj",
            "date_str": "2026-07-20", "triage_level": 2, "tag": "vip", "level_2_summary": "sum",
        }]

        def fake_get_unread_emails(account=None, limit=None):
            return rows_gmail if account == "gmail@test.com" else []

        db.get_unread_emails.side_effect = fake_get_unread_emails

        engine = MagicMock(spec=EmailTriageEngine)
        settings = MagicMock()
        settings.gmail_account = "gmail@test.com"
        settings.imap_login = "imap@test.com"

        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (db, engine, settings))

        gmail_ctor = MagicMock(side_effect=AssertionError("GmailClient should not be constructed"))
        imap_ctor = MagicMock(side_effect=AssertionError("IMAPClient should not be constructed"))
        monkeypatch.setattr(mcp_server, "GmailClient", gmail_ctor)
        monkeypatch.setattr(mcp_server, "IMAPClient", imap_ctor)

        result = mcp_server.fetch_and_process_unread(max_per_source=5, days=30, profile="default")

        gmail_ctor.assert_not_called()
        imap_ctor.assert_not_called()
        assert "VIP" in result
        assert "Subj" in result

    def test_pending_triage_row_does_not_crash(self, monkeypatch):
        db = MagicMock(spec=EmailDB)
        pending_row = {
            "message_id": "<p1@test.com>", "sender": "s@x.com", "subject": "Pending Subj",
            "date_str": "2026-07-20", "triage_level": None, "tag": None,
        }

        def fake_get_unread_emails(account=None, limit=None):
            return [pending_row] if account == "gmail@test.com" else []

        db.get_unread_emails.side_effect = fake_get_unread_emails

        engine = MagicMock(spec=EmailTriageEngine)
        settings = MagicMock()
        settings.gmail_account = "gmail@test.com"
        settings.imap_login = "imap@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (db, engine, settings))

        result = mcp_server.fetch_and_process_unread(max_per_source=5, days=30, profile="default")

        assert "Pending Background Triage" in result
        assert "Pending Subj" in result


class TestTriggerDownloadAndLastDownloadTime:
    def test_trigger_download_single_profile(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "sync_profile", MagicMock(return_value={"profile": "default", "status": "ok"}))
        monkeypatch.setattr(mcp_server, "sync_all_profiles", MagicMock(side_effect=AssertionError("should not be called")))

        result = mcp_server.trigger_download(profile="default")

        assert result["status"] == "ok"

    def test_trigger_download_all_profiles(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "sync_all_profiles", MagicMock(return_value={"profiles": {}}))
        monkeypatch.setattr(mcp_server, "sync_profile", MagicMock(side_effect=AssertionError("should not be called")))

        result = mcp_server.trigger_download(profile="all")

        assert "profiles" in result

    def test_get_last_download_time_single_profile(self, monkeypatch):
        db = MagicMock(spec=EmailDB)
        db.get_sync_summary.side_effect = lambda acct: {"account": acct}
        settings = MagicMock()
        settings.gmail_account = "gmail@test.com"
        settings.imap_login = "imap@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (db, MagicMock(), settings))

        result = mcp_server.get_last_download_time(profile="default")

        assert result["gmail"]["account"] == "gmail@test.com"
        assert result["imap"]["account"] == "imap@test.com"

    def test_get_last_download_time_all_profiles(self, monkeypatch):
        db = MagicMock(spec=EmailDB)
        db.get_sync_summary.return_value = None
        settings = MagicMock()
        settings.gmail_account = "g@test.com"
        settings.imap_login = "i@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (db, MagicMock(), settings))
        monkeypatch.setattr(mcp_server, "list_profile_names", lambda: ["default", "other"])

        result = mcp_server.get_last_download_time(profile="all")

        assert set(result["profiles"].keys()) == {"default", "other"}


class TestLoadTokenProfileMapReusesHelper:
    def test_delegates_to_list_profile_names(self, monkeypatch):
        fake_list = MagicMock(return_value=["default"])
        monkeypatch.setattr(mcp_server, "list_profile_names", fake_list)

        mcp_server.load_token_profile_map()

        fake_list.assert_called_once()


class TestStopEventSyncAccount:
    def test_already_stopped_skips_live_fetch(self):
        db = MagicMock(spec=EmailDB)
        engine = MagicMock(spec=EmailTriageEngine)
        client = MagicMock(spec=GmailClient)
        settings = MagicMock()
        settings.triage.confidence_threshold = 0.8
        stop_event = threading.Event()
        stop_event.set()

        summary = mcp_server.sync_account(db, engine, settings, client, "acct@test.com", None, 7, stop_event=stop_event)

        assert summary["status"] == "stopped"
        client.fetch_unread_messages.assert_not_called()
        db.save_sync_summary.assert_not_called()

    def test_stops_mid_loop(self):
        db = MagicMock(spec=EmailDB)
        db.get_unread_message_ids.return_value = set()
        db.get_cached_result.return_value = None
        engine = MagicMock(spec=EmailTriageEngine)
        engine.is_vip_sender.return_value = False
        engine.run_level_0_static.return_value = (True, "noise")

        client = MagicMock(spec=GmailClient)
        emails = [make_email("<a@test.com>", eid="1"), make_email("<b@test.com>", eid="2")]
        client.fetch_unread_messages.return_value = emails
        client.fetch_full_body.return_value = "body"

        settings = MagicMock()
        settings.triage.confidence_threshold = 0.8

        stop_event = threading.Event()

        def stop_after_first(*args, **kwargs):
            stop_event.set()
            return "body"

        client.fetch_full_body.side_effect = stop_after_first

        summary = mcp_server.sync_account(db, engine, settings, client, "acct@test.com", None, 7, stop_event=stop_event)

        assert summary["downloaded"] == 1
        assert summary["status"] == "stopped"
        db.save_sync_summary.assert_called_once()


class TestSyncProfileStop:
    def test_stop_before_gmail_skips_both_clients(self, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        fake_settings = MagicMock()
        fake_settings.gmail_account = "gmail@test.com"
        fake_settings.imap_login = "imap@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, fake_settings))

        gmail_ctor = MagicMock(side_effect=AssertionError("GmailClient should not be constructed"))
        imap_ctor = MagicMock(side_effect=AssertionError("IMAPClient should not be constructed"))
        monkeypatch.setattr(mcp_server, "GmailClient", gmail_ctor)
        monkeypatch.setattr(mcp_server, "IMAPClient", imap_ctor)

        profile = "stop-before-start-profile"
        mcp_server._get_stop_event(profile).set()

        result = mcp_server.sync_profile(profile)

        assert result["status"] == "stopped"
        gmail_ctor.assert_not_called()
        imap_ctor.assert_not_called()

    def test_stop_after_gmail_skips_imap(self, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        fake_settings = MagicMock()
        fake_settings.gmail_account = "gmail@test.com"
        fake_settings.imap_login = "imap@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, fake_settings))
        monkeypatch.setattr(mcp_server, "GmailClient", lambda settings_instance: MagicMock(spec=GmailClient))

        imap_ctor = MagicMock(side_effect=AssertionError("IMAPClient should not be constructed"))
        monkeypatch.setattr(mcp_server, "IMAPClient", imap_ctor)

        profile = "stop-after-gmail-profile"

        def fake_sync_account(db, engine, settings, client, account_label, max_results, days, stop_event=None):
            stop_event.set()
            return {"account": account_label, "status": "stopped"}

        monkeypatch.setattr(mcp_server, "sync_account", fake_sync_account)

        result = mcp_server.sync_profile(profile)

        assert result["status"] == "stopped"
        assert "imap" not in result
        imap_ctor.assert_not_called()

    def test_stop_event_cleared_after_run(self, monkeypatch):
        fake_db = MagicMock(spec=EmailDB)
        fake_engine = MagicMock(spec=EmailTriageEngine)
        fake_settings = MagicMock()
        fake_settings.gmail_account = "gmail@test.com"
        fake_settings.imap_login = "imap@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (fake_db, fake_engine, fake_settings))
        monkeypatch.setattr(mcp_server, "GmailClient", lambda settings_instance: MagicMock(spec=GmailClient))
        monkeypatch.setattr(mcp_server, "IMAPClient", lambda settings_instance: MagicMock(spec=IMAPClient))
        monkeypatch.setattr(mcp_server, "sync_account", lambda *a, **k: {"ok": True})

        profile = "clear-after-run-profile"
        mcp_server._get_stop_event(profile).set()

        result = mcp_server.sync_profile(profile)

        assert result["status"] == "stopped"
        assert mcp_server._get_stop_event(profile).is_set() is False


class TestProfileStatusHelper:
    def test_reflects_lock_and_stop_state(self, monkeypatch):
        db = MagicMock(spec=EmailDB)
        db.get_sync_summary.return_value = None
        settings = MagicMock()
        settings.gmail_account = "g@test.com"
        settings.imap_login = "i@test.com"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (db, MagicMock(), settings))

        profile = "status-helper-profile"
        status = mcp_server._profile_status(profile)
        assert status["running"] is False
        assert status["stop_requested"] is False

        lock = mcp_server._get_profile_lock(profile)
        lock.acquire()
        mcp_server._get_stop_event(profile).set()
        try:
            status = mcp_server._profile_status(profile)
            assert status["running"] is True
            assert status["stop_requested"] is True
        finally:
            lock.release()
            mcp_server._get_stop_event(profile).clear()

    def test_configured_flag_true_for_real_settings(self, monkeypatch):
        settings = make_fake_settings(gmail_account="real@gmail.com", imap_login="real@domain.com")
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (MagicMock(spec=EmailDB), MagicMock(), settings))

        assert mcp_server._profile_status("default")["configured"] is True

    def test_configured_flag_false_for_placeholder_settings(self, monkeypatch):
        settings = make_fake_settings(
            gmail_account=mcp_server._PLACEHOLDER_GMAIL_ACCOUNT,
            imap_login=mcp_server._PLACEHOLDER_IMAP_LOGIN,
        )
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (MagicMock(spec=EmailDB), MagicMock(), settings))

        assert mcp_server._profile_status("default")["configured"] is False

    def test_configured_flag_true_if_only_one_side_is_set(self, monkeypatch):
        settings = make_fake_settings(gmail_account="real@gmail.com", imap_login=mcp_server._PLACEHOLDER_IMAP_LOGIN)
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (MagicMock(spec=EmailDB), MagicMock(), settings))

        assert mcp_server._profile_status("default")["configured"] is True


class TestDashboardStatusFiltering:
    def test_unconfigured_default_is_hidden(self, monkeypatch):
        placeholder_settings = make_fake_settings(
            gmail_account=mcp_server._PLACEHOLDER_GMAIL_ACCOUNT,
            imap_login=mcp_server._PLACEHOLDER_IMAP_LOGIN,
        )
        configured_settings = make_fake_settings(gmail_account="stone@gmail.com", imap_login="stone@domain.com")

        def fake_get_resources(name):
            db = MagicMock(spec=EmailDB)
            db.get_sync_summary.return_value = None
            db.get_email_counts.return_value = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
            settings = placeholder_settings if name == "default" else configured_settings
            return (db, MagicMock(), settings)

        monkeypatch.setattr(mcp_server, "get_resources", fake_get_resources)
        monkeypatch.setattr(mcp_server, "list_profile_names", lambda: ["default", "stone"])

        status = mcp_server._dashboard_status()

        assert set(status["profiles"].keys()) == {"stone"}

    def test_configured_default_is_shown(self, monkeypatch):
        configured_settings = make_fake_settings(gmail_account="real@gmail.com", imap_login="real@domain.com")

        def fake_get_resources(name):
            db = MagicMock(spec=EmailDB)
            db.get_sync_summary.return_value = None
            db.get_email_counts.return_value = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
            return (db, MagicMock(), configured_settings)

        monkeypatch.setattr(mcp_server, "get_resources", fake_get_resources)
        monkeypatch.setattr(mcp_server, "list_profile_names", lambda: ["default"])

        status = mcp_server._dashboard_status()

        assert set(status["profiles"].keys()) == {"default"}

    def test_unconfigured_named_profile_is_still_shown(self, monkeypatch):
        placeholder_settings = make_fake_settings(
            gmail_account=mcp_server._PLACEHOLDER_GMAIL_ACCOUNT,
            imap_login=mcp_server._PLACEHOLDER_IMAP_LOGIN,
        )

        def fake_get_resources(name):
            db = MagicMock(spec=EmailDB)
            db.get_sync_summary.return_value = None
            db.get_email_counts.return_value = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
            return (db, MagicMock(), placeholder_settings)

        monkeypatch.setattr(mcp_server, "get_resources", fake_get_resources)
        monkeypatch.setattr(mcp_server, "list_profile_names", lambda: ["freshly-created-profile"])

        status = mcp_server._dashboard_status()

        # Only "default" is special-cased -- a newly created named profile is shown even mid-setup.
        assert set(status["profiles"].keys()) == {"freshly-created-profile"}


class TestProfileConfigMasking:
    def test_secrets_are_masked_not_leaked(self, monkeypatch):
        settings = make_fake_settings(
            triage_api_key="triage-secret", summary_api_key="summary-secret",
            imap_password="imap-secret", active_smtp_password="smtp-secret",
        )
        settings.triage.tei_api_key = "tei-secret"
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (MagicMock(), MagicMock(), settings))

        cfg = mcp_server._profile_config("default")

        for secret in ("triage-secret", "summary-secret", "imap-secret", "smtp-secret", "tei-secret"):
            assert secret not in str(cfg.values())
        assert cfg["triage_api_key"] == "•••• (set)"
        assert cfg["summary_api_key"] == "•••• (set)"
        assert cfg["imap_password"] == "•••• (set)"
        assert cfg["smtp_password"] == "•••• (set)"
        assert cfg["tei_api_key"] == "•••• (set)"

    def test_unset_secrets_show_not_set(self, monkeypatch):
        settings = make_fake_settings()
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (MagicMock(), MagicMock(), settings))

        cfg = mcp_server._profile_config("default")

        assert cfg["triage_api_key"] == "(not set)"
        assert cfg["imap_password"] == "(not set)"

    def test_non_secret_fields_pass_through(self, monkeypatch):
        settings = make_fake_settings()
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (MagicMock(), MagicMock(), settings))

        cfg = mcp_server._profile_config("default")

        assert cfg["triage_model"] == "model-a"
        assert cfg["confidence_threshold"] == 0.8
        assert cfg["scheduler_interval"] == "15m"


class TestDashboardRoutes:
    @pytest.fixture
    def client(self, monkeypatch):
        from starlette.testclient import TestClient

        db = MagicMock(spec=EmailDB)
        db.get_sync_summary.return_value = None
        db.get_email_counts.return_value = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
        settings = make_fake_settings(gmail_account="gmail@test.com", imap_login="imap@test.com", triage_api_key="super-secret")
        monkeypatch.setattr(mcp_server, "get_resources", lambda profile: (db, MagicMock(), settings))
        monkeypatch.setattr(mcp_server, "list_profile_names", lambda: ["default"])

        return TestClient(mcp_server.mcp.sse_app())

    def test_dashboard_returns_html(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Sync Dashboard" in response.text

    def test_api_status_shape(self, client):
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "scheduler" in data
        assert set(data["profiles"].keys()) == {"default"}
        profile_data = data["profiles"]["default"]
        assert profile_data["gmail"]["account"] == "gmail@test.com"
        assert profile_data["gmail"]["counts"]["total"] == 0
        assert profile_data["gmail"]["progress"] is None
        assert "config" in profile_data
        assert profile_data["config"]["gmail_account"] == "gmail@test.com"
        # secrets must never appear in the payload, only a presence indicator
        assert "super-secret" not in response.text
        assert profile_data["config"]["triage_api_key"] == "•••• (set)"
        assert profile_data["config"]["imap_password"] == "(not set)"

    def test_sync_start_and_stop_do_not_block(self, client, monkeypatch):
        started = threading.Event()
        monkeypatch.setattr(mcp_server, "sync_profile", lambda profile: started.set())

        response = client.post("/api/sync/start?profile=default")
        assert response.status_code == 200
        assert response.json() == {"status": "started", "profile": "default"}
        assert started.wait(timeout=2)

        response = client.post("/api/sync/stop?profile=default")
        assert response.status_code == 200
        assert response.json() == {"status": "stop_requested", "profile": "default"}
        assert mcp_server._get_stop_event("default").is_set() is True
        mcp_server._get_stop_event("default").clear()

    def test_api_logs_returns_buffered_lines(self, client):
        mcp_server._log_buffer.clear()
        logging.getLogger("email_triage.test_probe").warning("dashboard log route probe")

        response = client.get("/api/logs")

        assert response.status_code == 200
        data = response.json()
        assert any("dashboard log route probe" in entry["line"] for entry in data["logs"])
        assert data["last_seq"] == data["logs"][-1]["seq"]

    def test_api_logs_since_filters_out_old_lines(self, client):
        mcp_server._log_buffer.clear()
        logging.getLogger("email_triage.test_probe").warning("old line")
        first_seq = mcp_server._log_buffer[-1][0]
        logging.getLogger("email_triage.test_probe").warning("new line")

        response = client.get(f"/api/logs?since={first_seq}")

        lines = [entry["line"] for entry in response.json()["logs"]]
        assert not any("old line" in l for l in lines)
        assert any("new line" in l for l in lines)


class TestLogBufferHelpers:
    def test_sse_encode_single_line(self):
        assert mcp_server._sse_encode("hello") == "data: hello\n\n"

    def test_sse_encode_multi_line_traceback(self):
        encoded = mcp_server._sse_encode("line1\nline2")
        assert encoded == "data: line1\ndata: line2\n\n"

    def test_log_lines_since_filters_by_seq(self):
        mcp_server._log_buffer.clear()
        seq1 = next(mcp_server._log_seq)
        mcp_server._log_buffer.append((seq1, "first"))
        seq2 = next(mcp_server._log_seq)
        mcp_server._log_buffer.append((seq2, "second"))

        assert [e["line"] for e in mcp_server._log_lines_since(0)] == ["first", "second"]
        assert [e["line"] for e in mcp_server._log_lines_since(seq1)] == ["second"]

    def test_dashboard_log_handler_captures_records(self):
        mcp_server._log_buffer.clear()

        logging.getLogger("email_triage.test_probe").warning("probe message xyz")

        lines = [entry["line"] for entry in mcp_server._log_lines_since(0)]
        assert any("probe message xyz" in line for line in lines)

    def test_buffer_is_bounded(self):
        mcp_server._log_buffer.clear()
        test_logger = logging.getLogger("email_triage.test_probe")
        for i in range(mcp_server._log_buffer.maxlen + 50):
            test_logger.warning("line %d", i)

        assert len(mcp_server._log_buffer) == mcp_server._log_buffer.maxlen
