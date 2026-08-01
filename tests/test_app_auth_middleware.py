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


SESSION_ID_HEX = "4d6665b25a764fde8eea028793e27b13"


def _make_router_app(captured_profiles):
    """A single inner app standing in for the whole downstream Starlette app --
    in the real deployment, one AppAuthMiddleware instance wraps the entire
    app, so the long-lived GET /sse connection and a concurrent POST
    /messages/ both flow through the same middleware instance and share its
    _session_profiles dict. The GET branch blocks (via scope["close_event"])
    until the test tells it to finish, exactly like a real SSE stream stays
    open for the life of the session."""

    async def router_app(scope, receive, send):
        if scope["path"].startswith("/sse"):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({
                "type": "http.response.body",
                "body": b"event: endpoint\r\ndata: /messages/?session_id=" + SESSION_ID_HEX.encode() + b"\r\n\r\n",
                "more_body": True,
            })
            await scope["close_event"].wait()
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            captured_profiles.append(mcp_server.current_profile.get())
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"Accepted", "more_body": False})

    return router_app


async def _run_concurrent_sse_and_post_scenario(middleware, sse_scope, post_scope):
    """Starts the GET /sse connection as a background task, waits for the
    middleware to observe its session_id, fires the POST while the SSE
    connection is still open, then closes the SSE connection and returns
    (post_sent, sse_sent)."""
    close_event = asyncio.Event()
    sse_scope["close_event"] = close_event

    sse_sent = []

    async def sse_receive():
        return {"type": "http.request"}

    async def sse_send(message):
        sse_sent.append(message)

    task = asyncio.create_task(middleware(sse_scope, sse_receive, sse_send))
    try:
        for _ in range(200):
            if SESSION_ID_HEX in middleware._session_profiles:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("middleware never recorded the session_id from the SSE endpoint event")

        post_sent = []

        async def post_receive():
            return {"type": "http.request"}

        async def post_send(message):
            post_sent.append(message)

        await middleware(post_scope, post_receive, post_send)
    finally:
        close_event.set()
        await task

    return post_sent, sse_sent


def test_messages_post_without_token_allowed_for_already_authenticated_session(app_db):
    # Reproduces the real-world bug: a client authenticates the initial GET
    # /sse with a valid token (as most SSE-based MCP clients do), then POSTs
    # to /messages/?session_id=... -- the endpoint URL the mcp SDK itself
    # handed back -- with no token at all, since the SDK never gives clients
    # a way to carry it forward. That POST must not 401.
    with appdb.get_conn(app_db) as conn:
        user = us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        raw, _row = mt.create_token(conn, user["id"])

    captured_profiles = []
    middleware = mcp_server.AppAuthMiddleware(_make_router_app(captured_profiles), token_map={})

    sse_scope = _make_scope(path="/sse", token=raw)
    post_scope = {
        "type": "http", "path": "/messages/", "headers": [],
        "query_string": f"session_id={SESSION_ID_HEX}".encode(),
    }

    post_sent, sse_sent = asyncio.run(
        _run_concurrent_sse_and_post_scenario(middleware, sse_scope, post_scope)
    )

    assert sse_sent[0]["status"] == 200
    assert post_sent[0]["status"] == 202
    assert captured_profiles == ["bob"]
    # Cleaned up once the SSE connection (which the scenario helper closes at the end) finishes.
    assert SESSION_ID_HEX not in middleware._session_profiles


def test_messages_post_with_unknown_session_id_still_401s(app_db):
    middleware = mcp_server.AppAuthMiddleware(_dummy_app, token_map={})
    scope = {
        "type": "http", "path": "/messages/", "headers": [],
        "query_string": b"session_id=deadbeefdeadbeefdeadbeefdeadbeef",
    }
    sent = run_middleware(middleware, scope)
    assert sent[0]["status"] == 401
