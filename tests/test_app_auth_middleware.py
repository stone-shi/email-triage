import asyncio
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import mcp_server
import mcp_tokens_store as mt
import secretstore
import users_store as us


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    appdb.init_app_db(db_path)
    yield db_path
    secretstore.reset_key_cache()


async def _dummy_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


def _make_scope(path="/sse", token=None):
    query_string = f"token={token}".encode() if token else b""
    return {"type": "http", "path": path, "headers": [], "query_string": query_string}


def run_middleware(middleware, scope):
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def test_valid_db_token_sets_profile_and_passes_through(app_db):
    with appdb.get_conn(app_db) as conn:
        user = us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        raw, _row = mt.create_token(conn, user["id"])

    captured = {}

    async def inner_app(scope, receive, send):
        captured["profile"] = mcp_server.current_profile.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = mcp_server.AppAuthMiddleware(inner_app, token_map={})
    sent = run_middleware(middleware, _make_scope(token=raw))
    assert sent[0]["status"] == 200
    assert captured["profile"] == "bob"


def test_revoked_db_token_without_legacy_fallback_401s(app_db):
    with appdb.get_conn(app_db) as conn:
        user = us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        raw, row = mt.create_token(conn, user["id"])
        mt.revoke_token(conn, user["id"], row["id"])

    middleware = mcp_server.AppAuthMiddleware(_dummy_app, token_map={})
    sent = run_middleware(middleware, _make_scope(token=raw))
    assert sent[0]["status"] == 401


def test_no_token_401s(app_db):
    middleware = mcp_server.AppAuthMiddleware(_dummy_app, token_map={})
    sent = run_middleware(middleware, _make_scope(token=None))
    assert sent[0]["status"] == 401


def test_falls_back_to_legacy_token_map(app_db, monkeypatch):
    # No DB token matches, but the legacy .env-scraped map does.
    monkeypatch.setattr(mcp_server, "load_token_profile_map", lambda: {"legacy-token": "stone"})

    captured = {}

    async def inner_app(scope, receive, send):
        captured["profile"] = mcp_server.current_profile.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = mcp_server.AppAuthMiddleware(inner_app, token_map={})
    sent = run_middleware(middleware, _make_scope(token="legacy-token"))
    assert sent[0]["status"] == 200
    assert captured["profile"] == "stone"


def test_non_sse_non_messages_path_passes_through_unauthenticated(app_db):
    sent = run_middleware(mcp_server.AppAuthMiddleware(_dummy_app, token_map={}), _make_scope(path="/version"))
    assert sent[0]["status"] == 200


def test_messages_path_is_also_guarded(app_db):
    sent = run_middleware(mcp_server.AppAuthMiddleware(_dummy_app, token_map={}), _make_scope(path="/messages/"))
    assert sent[0]["status"] == 401
