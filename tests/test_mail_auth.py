import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mail_auth


class TestPasswordMailAuth:
    def test_attach_calls_login(self):
        box = MagicMock()
        auth = mail_auth.PasswordMailAuth("hunter2")
        result = auth.attach(box, "bob@example.com")
        box.login.assert_called_once_with("bob@example.com", "hunter2")
        assert result is box.login.return_value


class TestXOAuth2MailAuth:
    def test_attach_calls_xoauth2_with_fresh_token(self, monkeypatch):
        fake_token_service = MagicMock()
        fake_token_service.access_token.return_value = "live-access-token"
        monkeypatch.setitem(sys.modules, "token_service", fake_token_service)

        box = MagicMock()
        auth = mail_auth.XOAuth2MailAuth(integration_id=42)
        result = auth.attach(box, "bob@zoho.com")

        fake_token_service.access_token.assert_called_once_with(42)
        box.xoauth2.assert_called_once_with("bob@zoho.com", "live-access-token")
        assert result is box.xoauth2.return_value


class TestPasswordSmtpAuth:
    def test_authenticate_calls_login(self):
        server = MagicMock()
        auth = mail_auth.PasswordSmtpAuth("hunter2")
        auth.authenticate(server, "bob@example.com")
        server.login.assert_called_once_with("bob@example.com", "hunter2")


class TestXOAuth2SmtpAuth:
    def test_authenticate_calls_auth_with_xoauth2_mechanism(self, monkeypatch):
        fake_token_service = MagicMock()
        fake_token_service.access_token.return_value = "live-access-token"
        monkeypatch.setitem(sys.modules, "token_service", fake_token_service)

        server = MagicMock()
        auth = mail_auth.XOAuth2SmtpAuth(integration_id=7)
        auth.authenticate(server, "bob@zoho.com")

        server.auth.assert_called_once()
        args, kwargs = server.auth.call_args
        assert args[0] == "XOAUTH2"
        sasl_string = args[1]()
        assert sasl_string == "user=bob@zoho.com\x01auth=Bearer live-access-token\x01\x01"
        assert kwargs.get("initial_response_ok") is True
