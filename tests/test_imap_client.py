import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import imap_client
from imap_client import IMAPClient
from mail_auth import PasswordMailAuth


def make_client():
    client = IMAPClient.__new__(IMAPClient)
    client.settings = MagicMock()
    client.host = "imap.test.com"
    client.port = 993
    client.login_user = "user@test.com"
    client.password = "secret"
    client.mail_auth = PasswordMailAuth("secret")
    return client


def make_fake_message(uid, text=None, html=None):
    msg = MagicMock()
    msg.uid = uid
    msg.text = text
    msg.html = html
    return msg


def make_fake_header_message(uid, message_id=None, sender="s@x.com", subject="Subj", date="2026-01-01", desc=None):
    msg = MagicMock()
    msg.uid = uid
    msg.headers = {"message-id": [message_id or f"<{uid}@test.com>"]}
    msg.from_ = sender
    msg.subject = subject
    msg.date = date
    msg.desc = desc
    return msg


def patch_mailbox(monkeypatch, fake_mailbox):
    fake_mailbox_cm = MagicMock()
    fake_mailbox_cm.__enter__.return_value = fake_mailbox
    fake_mailbox_cm.__exit__.return_value = False
    mailbox_ctor = MagicMock()
    mailbox_ctor.return_value.login.return_value = fake_mailbox_cm
    monkeypatch.setattr(imap_client, "MailBox", mailbox_ctor)
    return mailbox_ctor


class TestFetchFullBodiesBatch:
    def test_fetches_multiple_bodies_in_one_connection(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.return_value = [
            make_fake_message("1", text="body one"),
            make_fake_message("2", text="body two"),
        ]
        mailbox_ctor = patch_mailbox(monkeypatch, fake_mailbox)

        result = client.fetch_full_bodies_batch(["1", "2"])

        assert result == {"1": "body one", "2": "body two"}
        mailbox_ctor.assert_called_once_with("imap.test.com", port=993)
        mailbox_ctor.return_value.login.assert_called_once_with("user@test.com", "secret")

    def test_falls_back_to_html_when_no_text(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.return_value = [make_fake_message("1", text=None, html="<p>hi</p>")]
        patch_mailbox(monkeypatch, fake_mailbox)

        result = client.fetch_full_bodies_batch(["1"])

        assert result == {"1": "<p>hi</p>"}

    def test_missing_body_defaults_to_empty_string(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.return_value = [make_fake_message("1", text=None, html=None)]
        patch_mailbox(monkeypatch, fake_mailbox)

        result = client.fetch_full_bodies_batch(["1"])

        assert result == {"1": ""}

    def test_empty_input_returns_empty_dict_without_connecting(self, monkeypatch):
        client = make_client()
        mailbox_ctor = MagicMock()
        monkeypatch.setattr(imap_client, "MailBox", mailbox_ctor)

        result = client.fetch_full_bodies_batch([])

        assert result == {}
        mailbox_ctor.assert_not_called()

    def test_chunks_large_uid_lists(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.side_effect = [
            [make_fake_message(str(i), text=f"body {i}") for i in range(100)],
            [make_fake_message("100", text="body 100")],
        ]
        patch_mailbox(monkeypatch, fake_mailbox)

        uids = [str(i) for i in range(101)]
        result = client.fetch_full_bodies_batch(uids, chunk_size=100)

        assert len(result) == 101
        assert fake_mailbox.fetch.call_count == 2

    def test_connection_failure_returns_empty_dict(self, monkeypatch):
        client = make_client()
        mailbox_ctor = MagicMock()
        mailbox_ctor.return_value.login.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(imap_client, "MailBox", mailbox_ctor)

        result = client.fetch_full_bodies_batch(["1"])

        assert result == {}

    def test_chunk_failure_does_not_abort_remaining_chunks(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.side_effect = [
            RuntimeError("server hiccup"),
            [make_fake_message("2", text="body two")],
        ]
        patch_mailbox(monkeypatch, fake_mailbox)

        result = client.fetch_full_bodies_batch(["1"], chunk_size=1)
        # only "1" was requested in this call, so let's instead verify two chunks combine correctly
        client2 = make_client()
        fake_mailbox2 = MagicMock()
        fake_mailbox2.fetch.side_effect = [
            RuntimeError("server hiccup"),
            [make_fake_message("2", text="body two")],
        ]
        patch_mailbox(monkeypatch, fake_mailbox2)
        result2 = client2.fetch_full_bodies_batch(["1", "2"], chunk_size=1)

        assert result2 == {"2": "body two"}


class TestFetchAllHeaders:
    def test_fetches_all_messages_not_scoped_to_unread(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.return_value = [
            make_fake_header_message("1", subject="First"),
            make_fake_header_message("2", subject="Second"),
        ]
        patch_mailbox(monkeypatch, fake_mailbox)

        result = client.fetch_all_headers()

        assert [r["subject"] for r in result] == ["First", "Second"]
        assert [r["id"] for r in result] == ["1", "2"]
        _, kwargs = fake_mailbox.fetch.call_args
        assert kwargs.get("headers_only") is True
        assert kwargs.get("mark_seen") is False

    def test_maps_message_shape_same_as_unread_headers(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.return_value = [
            make_fake_header_message("42", message_id="<full@test.com>", sender="a@b.com", subject="Hi", date="2026-02-01"),
        ]
        patch_mailbox(monkeypatch, fake_mailbox)

        result = client.fetch_all_headers()

        assert result == [{
            "id": "42", "message_id": "<full@test.com>", "sender": "a@b.com",
            "subject": "Hi", "date": "2026-02-01", "snippet": "Subject: Hi", "account": "user@test.com",
        }]

    def test_respects_max_results_limit(self, monkeypatch):
        client = make_client()
        fake_mailbox = MagicMock()
        fake_mailbox.fetch.return_value = [make_fake_header_message("1")]
        patch_mailbox(monkeypatch, fake_mailbox)

        client.fetch_all_headers(max_results=5)

        _, kwargs = fake_mailbox.fetch.call_args
        assert kwargs.get("limit") == 5

    def test_connection_failure_returns_empty_list(self, monkeypatch):
        client = make_client()
        mailbox_ctor = MagicMock()
        mailbox_ctor.return_value.login.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(imap_client, "MailBox", mailbox_ctor)

        result = client.fetch_all_headers()

        assert result == []


class TestFetchFullEmail:
    def test_resolves_by_message_id_and_fetches_body(self):
        client = make_client()
        client._find_message = MagicMock(return_value={
            "uid": "42",
            "message_id": "<rfc123@example.com>",
            "from": "sender@example.com",
            "subject": "Hello",
        })
        client.fetch_full_body = MagicMock(return_value="full body text")

        result = client.fetch_full_email("<rfc123@example.com>")

        client._find_message.assert_called_once_with("<rfc123@example.com>")
        client.fetch_full_body.assert_called_once_with("42")
        assert result == {
            "id": "42",
            "message_id": "<rfc123@example.com>",
            "sender": "sender@example.com",
            "subject": "Hello",
            "date": "",
            "body": "full body text",
            "account": "user@test.com",
        }
