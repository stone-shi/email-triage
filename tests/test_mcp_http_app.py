"""End-to-end coverage for mcp_server.build_http_app(): the combined Starlette
app served under SSE transport mode, which mounts Streamable HTTP at /mcp
alongside the legacy /sse + /messages/ endpoints. Runs through a real
TestClient (not just direct middleware calls) so the session-manager lifespan
wiring and actual MCP protocol handshake are exercised too.
"""

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

INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    appdb.init_app_db(db_path)
    yield db_path
    secretstore.reset_key_cache()


@pytest.fixture
def token(app_db):
    with appdb.get_conn(app_db) as conn:
        user = us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)
        raw, _row = mt.create_token(conn, user["id"])
    return raw


@pytest.fixture
def client(app_db, monkeypatch):
    from starlette.testclient import TestClient

    # mcp is a process-wide singleton and StreamableHTTPSessionManager.run() can
    # only ever be called once per instance -- reset the cached manager so each
    # test gets a fresh one instead of erroring on a second lifespan startup.
    monkeypatch.setattr(mcp_server.mcp, "_session_manager", None)

    app = mcp_server.build_http_app()
    app.add_middleware(mcp_server.AppAuthMiddleware, token_map={})
    with TestClient(app) as c:
        yield c


class TestStreamableHttpMount:
    def test_mcp_route_exists_at_the_sdk_default_path(self):
        app = mcp_server.build_http_app()
        paths = [getattr(r, "path", None) for r in app.router.routes]
        assert "/mcp" in paths
        assert mcp_server.mcp.settings.streamable_http_path == "/mcp"

    def test_mcp_route_is_registered_before_the_spa_catch_all(self):
        app = mcp_server.build_http_app()
        paths = [getattr(r, "path", None) for r in app.router.routes]
        mcp_index = paths.index("/mcp")
        catch_all_index = paths.index("/{full_path:path}")
        assert mcp_index < catch_all_index

    def test_legacy_sse_and_messages_routes_still_present(self):
        app = mcp_server.build_http_app()
        paths = [getattr(r, "path", None) for r in app.router.routes]
        assert "/sse" in paths
        assert "/messages" in paths or "/messages/" in paths

    def test_no_token_401s(self, client):
        resp = client.post("/mcp", json=INITIALIZE_BODY, headers=MCP_HEADERS)
        assert resp.status_code == 401

    def test_valid_token_completes_the_mcp_handshake(self, client, token):
        resp = client.post(
            "/mcp", json=INITIALIZE_BODY, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert "mcp-session-id" in resp.headers
        assert '"serverInfo"' in resp.text

    def test_invalid_token_401s(self, client, token):
        resp = client.post(
            "/mcp", json=INITIALIZE_BODY, headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 401
