import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import oauth_flow


@pytest.fixture(autouse=True)
def _fixed_signing_key(monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", "a-fixed-test-signing-key")


class TestState:
    def test_roundtrip(self):
        state = oauth_flow.make_state(user_id=7, provider="gmail", nonce="abc123")
        payload = oauth_flow.parse_state(state)
        assert payload["user_id"] == 7
        assert payload["provider"] == "gmail"
        assert payload["nonce"] == "abc123"

    def test_tampered_signature_rejected(self):
        state = oauth_flow.make_state(user_id=7, provider="gmail", nonce="abc123")
        body, sig = state.split(".", 1)
        tampered = f"{body}.{'0' * len(sig)}"
        with pytest.raises(oauth_flow.OAuthError):
            oauth_flow.parse_state(tampered)

    def test_tampered_body_rejected(self):
        state = oauth_flow.make_state(user_id=7, provider="gmail", nonce="abc123")
        body, sig = state.split(".", 1)
        with pytest.raises(oauth_flow.OAuthError):
            oauth_flow.parse_state(f"{body}x.{sig}")

    def test_malformed_state_rejected(self):
        with pytest.raises(oauth_flow.OAuthError):
            oauth_flow.parse_state("not-a-valid-state")

    def test_expired_state_rejected(self, monkeypatch):
        monkeypatch.setattr(oauth_flow, "STATE_TTL_SECONDS", -1)
        state = oauth_flow.make_state(user_id=7, provider="gmail", nonce="abc123")
        with pytest.raises(oauth_flow.OAuthError):
            oauth_flow.parse_state(state)

    def test_different_signing_key_rejected(self, monkeypatch):
        state = oauth_flow.make_state(user_id=7, provider="gmail", nonce="abc123")
        monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", "a-different-key")
        with pytest.raises(oauth_flow.OAuthError):
            oauth_flow.parse_state(state)


class TestOAuthClient:
    def make_client(self):
        return oauth_flow.OAuthClient(
            provider="testprov",
            client_id="cid",
            client_secret="csecret",
            authorize_url="https://provider.example/auth",
            token_url="https://provider.example/token",
            redirect_uri="https://app.example/callback",
            scopes="scope-a scope-b",
            extra_authorize_params=(("access_type", "offline"),),
        )

    def test_build_authorize_url_includes_all_params(self):
        client = self.make_client()
        url = client.build_authorize_url(state="signed-state")
        assert url.startswith("https://provider.example/auth?")
        assert "client_id=cid" in url
        assert "state=signed-state" in url
        assert "access_type=offline" in url
        assert "scope=scope-a+scope-b" in url

    def test_exchange_code_success(self, monkeypatch):
        client = self.make_client()
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"access_token": "at", "refresh_token": "rt"}
        monkeypatch.setattr(oauth_flow.httpx, "post", lambda *a, **k: fake_response)

        result = client.exchange_code("auth-code")
        assert result == {"access_token": "at", "refresh_token": "rt"}

    def test_exchange_code_failure_raises(self, monkeypatch):
        client = self.make_client()
        fake_response = MagicMock(status_code=400, text="invalid_grant")
        monkeypatch.setattr(oauth_flow.httpx, "post", lambda *a, **k: fake_response)

        with pytest.raises(oauth_flow.OAuthError):
            client.exchange_code("bad-code")

    def test_refresh_success(self, monkeypatch):
        client = self.make_client()
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"access_token": "new-at"}
        monkeypatch.setattr(oauth_flow.httpx, "post", lambda *a, **k: fake_response)

        result = client.refresh("some-refresh-token")
        assert result == {"access_token": "new-at"}

    def test_refresh_failure_raises(self, monkeypatch):
        client = self.make_client()
        fake_response = MagicMock(status_code=401, text="invalid_grant: token revoked")
        monkeypatch.setattr(oauth_flow.httpx, "post", lambda *a, **k: fake_response)

        with pytest.raises(oauth_flow.OAuthError):
            client.refresh("revoked-token")
