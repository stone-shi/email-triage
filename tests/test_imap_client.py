import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import imap_client
from imap_client import IMAPClient


def make_client():
    client = IMAPClient.__new__(IMAPClient)
    client.settings = MagicMock()
    client.host = "imap.test.com"
    client.port = 993
    client.login_user = "user@test.com"
    client.password = "secret"
    return client


def make_fake_message(uid, text=None, html=None):
    msg = MagicMock()
    msg.uid = uid
    msg.text = text
    msg.html = html
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
