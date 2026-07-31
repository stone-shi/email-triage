import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import account_clients
import appdb
import integrations_store as ints
import secretstore
import users_store as us
from config import PLACEHOLDER_GMAIL_ACCOUNT, PLACEHOLDER_IMAP_LOGIN


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    appdb.init_app_db(path)
    with appdb.get_conn(path) as c:
        yield c


@pytest.fixture
def user(conn):
    return us.create_user(conn, username="bob", password="a_long_enough_password")


def fake_settings(**overrides):
    base = dict(
        gmail_account=PLACEHOLDER_GMAIL_ACCOUNT,
        imap_login=PLACEHOLDER_IMAP_LOGIN,
        imap_password="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestLegacyFallback:
    def test_no_accounts_when_unconfigured(self, conn, user):
        accounts = account_clients.clients_for_user(conn, user["id"], fake_settings())
        assert accounts == []

    def test_builds_gmail_and_imap_when_configured(self, conn, user, monkeypatch):
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock(name="gmail"))
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock(name="imap"))
        settings = fake_settings(gmail_account="bob@gmail.com", imap_login="bob@zoho.com", imap_password="secret")

        accounts = account_clients.clients_for_user(conn, user["id"], settings)

        assert {a.provider for a in accounts} == {"gmail", "imap"}
        assert {a.account for a in accounts} == {"bob@gmail.com", "bob@zoho.com"}
        assert all(a.integration_id is None for a in accounts)

    def test_placeholder_gmail_account_is_skipped(self, conn, user, monkeypatch):
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock())
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())
        settings = fake_settings(imap_login="bob@zoho.com", imap_password="secret")

        accounts = account_clients.clients_for_user(conn, user["id"], settings)

        assert [a.provider for a in accounts] == ["imap"]

    def test_imap_without_password_is_skipped(self, conn, user, monkeypatch):
        settings = fake_settings(imap_login="bob@zoho.com", imap_password="")
        accounts = account_clients.clients_for_user(conn, user["id"], settings)
        assert accounts == []


class TestDbBackedIntegrations:
    def test_builds_one_client_per_enabled_row(self, conn, user, monkeypatch):
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock())
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())

        ints.create_integration(
            conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com", auth_type="oauth",
            secret={"refresh_token": "rt"},
        )
        ints.create_integration(
            conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password",
            secret={"password": "hunter2"},
        )

        accounts = account_clients.clients_for_user(conn, user["id"], fake_settings())

        assert len(accounts) == 2
        assert {a.provider for a in accounts} == {"gmail", "imap"}
        assert all(a.integration_id is not None for a in accounts)

    def test_disabled_row_is_excluded(self, conn, user, monkeypatch):
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())
        row = ints.create_integration(
            conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password",
            secret={"password": "hunter2"},
        )
        ints.update_integration(conn, row["id"], enabled=False)

        accounts = account_clients.clients_for_user(conn, user["id"], fake_settings())
        assert accounts == []

    def test_triage_only_filter(self, conn, user, monkeypatch):
        monkeypatch.setattr(account_clients, "IMAPClient", lambda **kw: MagicMock())
        row = ints.create_integration(
            conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password",
            secret={"password": "hunter2"},
        )
        ints.update_integration(conn, row["id"], triage_enabled=False)

        assert account_clients.clients_for_user(conn, user["id"], fake_settings(), for_triage=True) == []
        assert len(account_clients.clients_for_user(conn, user["id"], fake_settings(), for_archive=True)) == 1

    def test_broken_integration_is_skipped_not_raised(self, conn, user, monkeypatch):
        def boom(**kw):
            raise RuntimeError("bad password")

        monkeypatch.setattr(account_clients, "IMAPClient", boom)
        ints.create_integration(
            conn, user_id=user["id"], provider="imap", account_key="bob@example.com", auth_type="password",
            secret={"password": "hunter2"},
        )

        accounts = account_clients.clients_for_user(conn, user["id"], fake_settings())

        assert accounts == []
        rows = ints.list_integrations(conn, user["id"])
        assert rows[0]["status"] == "error"
        assert "bad password" in rows[0]["last_test_error"]

    def test_two_gmail_accounts_both_build(self, conn, user, monkeypatch):
        monkeypatch.setattr(account_clients, "GmailClient", lambda **kw: MagicMock())
        ints.create_integration(
            conn, user_id=user["id"], provider="gmail", account_key="bob@gmail.com", auth_type="oauth",
            secret={"refresh_token": "rt1"},
        )
        ints.create_integration(
            conn, user_id=user["id"], provider="gmail", account_key="bob.work@gmail.com", auth_type="oauth",
            secret={"refresh_token": "rt2"},
        )

        accounts = account_clients.clients_for_user(conn, user["id"], fake_settings())
        assert len(accounts) == 2
        assert {a.account for a in accounts} == {"bob@gmail.com", "bob.work@gmail.com"}


class TestUnknownProvider:
    def test_build_client_for_integration_raises_for_unknown_provider(self):
        fake_row = {"provider": "carrier-pigeon"}
        with pytest.raises(ValueError):
            account_clients.build_client_for_integration(fake_row)
