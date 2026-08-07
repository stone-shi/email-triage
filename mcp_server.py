#!/usr/bin/env python3
"""
Model Context Protocol (MCP) Server for the Optimized Email Triage & Summarization Engine.
Exposes local database access, text search, and email triage pipelines to AI clients.
"""

import asyncio
import logging
import re
import sys
import threading
import itertools
import collections
import email.utils
from typing import List, Dict, Any, Optional
from pathlib import Path

# 1. Force stderr-only logging before importing other modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("email_triage.mcp_server")

# In-memory ring buffer of recent log lines, for the dashboard's live log stream.
# Each entry is (monotonic sequence number, formatted line) so SSE/poll clients can
# request only what's new since the last sequence number they saw.
_log_buffer: collections.deque = collections.deque(maxlen=500)
_log_seq = itertools.count(1)


class _DashboardLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_buffer.append((next(_log_seq), self.format(record)))
        except Exception:
            pass


_dashboard_log_handler = _DashboardLogHandler()
_dashboard_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_dashboard_log_handler)


def _log_lines_since(since_seq: int = 0) -> List[Dict[str, Any]]:
    """Buffered log lines with seq > since_seq, oldest first."""
    return [{"seq": seq, "line": line} for seq, line in _log_buffer if seq > since_seq]


def _sse_encode(line: str) -> str:
    """Encodes a (possibly multi-line, e.g. traceback) log line as one SSE 'data:' event."""
    return "\n".join(f"data: {part}" for part in line.splitlines()) + "\n\n"

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("The 'mcp' SDK is not installed in the current virtual environment.")
    logger.info("Please run: ./venv/bin/pip install mcp")
    sys.exit(1)

# Import core engine modules from the local workspace
from db import EmailDB
from triage import EmailTriageEngine
from gmail_client import GmailClient
from imap_client import IMAPClient
from config import settings, list_profile_names
import account_clients
import appdb
import integrations_store as ints
import mcp_tokens_store
import prompts_store
import quality_check
import users_store
import web_api
import web_integrations_api
import web_prompts_api
import web_quality_api
import web_static
from web_auth import CurrentIdentity, error_response, requires_active_user, requires_admin

# Initialize FastMCP server
from mcp.server.transport_security import TransportSecuritySettings

class RobustFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        cleaned_name = name
        while True:
            if cleaned_name.startswith("email_triage__"):
                cleaned_name = cleaned_name[len("email_triage__"):]
            elif cleaned_name.startswith("email-triage__"):
                cleaned_name = cleaned_name[len("email-triage__"):]
            else:
                break

        # Check if cleaned_name matches a registered tool name directly
        tools = getattr(self._tool_manager, "_tools", {})
        if cleaned_name in tools:
            return await super().call_tool(cleaned_name, arguments)

        # Fallback: check if any registered tool starts with cleaned_name (handles truncation)
        # We sort by length to select the shortest/base tool name first (avoiding alias conflicts)
        matching_tools = sorted(
            [t_name for t_name in tools if t_name.startswith(cleaned_name)],
            key=len
        )
        if matching_tools:
            logger.info("Fuzzy matched tool call '%s' (cleaned: '%s') to registered tool '%s'", name, cleaned_name, matching_tools[0])
            return await super().call_tool(matching_tools[0], arguments)

        return await super().call_tool(cleaned_name, arguments)

security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = RobustFastMCP(
    "Email Triage Engine",
    host=settings.mcp_host,
    port=settings.mcp_port,
    transport_security=security,
    warn_on_duplicate_tools=False
)

# New auth/user-management/settings dashboard routes (login, users, MCP tokens,
# global settings). The pre-existing dashboard status/sync/logs routes further
# down in this file are registered separately and are not yet gated by these --
# see mcp_server.py's module docstring / CLAUDE.md for the cutover plan.
web_api.register_web_routes(mcp)
web_integrations_api.register_integrations_routes(mcp)
web_prompts_api.register_prompts_routes(mcp)
web_quality_api.register_quality_routes(mcp)

import contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, StreamingResponse, RedirectResponse, Response
from starlette.requests import Request

# ContextVar to store the authenticated profile name for the current request
current_profile = contextvars.ContextVar("current_profile", default="default")

def load_token_profile_map() -> Dict[str, str]:
    """Scans root .env and all profile .env files to build a token-to-profile map."""
    token_map = {}
    workspace_root = Path(__file__).parent.resolve()
    
    # 1. Check root .env for default token
    root_env = workspace_root / ".env"
    if root_env.exists():
        try:
            with open(root_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "EMAIL_TRIAGE_PROFILE_TOKEN":
                            token_map[v.strip()] = "default"
        except Exception:
            pass
            
    # 2. Check profiles/ directories
    profiles_dir = workspace_root / "profiles"
    for profile_name in list_profile_names():
        profile_env = profiles_dir / profile_name / ".env"
        if profile_env.exists():
            try:
                with open(profile_env, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() == "EMAIL_TRIAGE_PROFILE_TOKEN":
                                token_map[v.strip()] = profile_name
            except Exception:
                pass
    return token_map

class MCPTokenAuthMiddleware:
    def __init__(self, app, token_map: Dict[str, str]):
        self.app = app
        self.token_map = token_map

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            from starlette.datastructures import Headers, QueryParams
            
            headers = Headers(scope=scope)
            path = scope.get("path", "")
            
            if path.startswith("/sse"):
                self.token_map = load_token_profile_map()
                token = None
                auth_header = headers.get("authorization")
                if auth_header and auth_header.lower().startswith("bearer "):
                    token = auth_header[7:].strip()
                if not token:
                    token = headers.get("x-profile-token")
                if not token:
                    query_params = QueryParams(scope.get("query_string", b"").decode("utf-8"))
                    token = query_params.get("token")
                    
                if not token or token not in self.token_map:
                    body = b'{"error":"Unauthorized: Invalid or missing profile token"}'
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("utf-8")),
                        ]
                    })
                    await send({
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False
                    })
                    return
                
                profile = self.token_map[token]
                token_t = current_profile.set(profile)
                try:
                    await self.app(scope, receive, send)
                    return
                finally:
                    current_profile.reset(token_t)

        await self.app(scope, receive, send)


class AppAuthMiddleware:
    """Guards /sse, /messages/, and /mcp (the MCP protocol endpoints). Resolves
    a per-user DB-backed mcp_tokens row first; if data/app.db doesn't exist
    yet or the token isn't found there, falls back to the legacy .env-scraped
    EMAIL_TRIAGE_PROFILE_TOKEN map (same lookup MCPTokenAuthMiddleware used)
    so existing MCP clients keep working unmodified until migrate_to_db.py has
    imported their tokens into the database.

    Also guards /messages/, which the middleware it replaces did not: tool
    calls execute inside the GET /sse request's task (that's where
    RobustFastMCP awaits the session), so the current_profile contextvar set
    there was already visible to a tool call -- but a POST to /messages/ was
    reaching the server with no auth check of its own, protected only by the
    session_id being hard to guess.

    That guard initially required a token on *every* request including
    /messages/ POSTs -- but the mcp SDK's own SSE transport (see
    mcp/server/sse.py::connect_sse) hands the client a bare
    "/messages/?session_id=<uuid>" endpoint with no way to carry the original
    token forward, and most SSE-based MCP clients only ever authenticate the
    initial GET /sse connection, never the follow-up POSTs. Requiring a token
    on /messages/ too therefore 401'd every real client. The fix: record which
    profile authenticated each session_id's originating GET /sse connection
    (by watching for the "endpoint" SSE event as it streams out, which is the
    only place the server-assigned session_id is ever exposed), and let a
    /messages/ POST through on session_id alone if it matches a still-open,
    already-authenticated session -- the session_id is only ever handed out
    over an SSE stream that itself required a valid token, and the mapping is
    torn down the moment that stream disconnects.

    /mcp (Streamable HTTP, mounted alongside /sse -- see mcp_server.py's
    __main__) doesn't need that same fallback: unlike SSE, every request a
    client makes -- the initial one and all follow-ups -- goes to the exact
    same URL, so a client that's configured to send a token at all sends it
    on every request. It's therefore just required outright, same as the
    original (pre-session-fallback) behavior for /sse.
    """

    def __init__(self, app, token_map: Dict[str, str]):
        self.app = app
        self.token_map = token_map
        self._session_profiles: Dict[str, str] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            from starlette.datastructures import Headers, QueryParams

            headers = Headers(scope=scope)
            path = scope.get("path", "")

            if path.startswith("/sse") or path.startswith("/messages") or path.startswith("/mcp"):
                token = None
                auth_header = headers.get("authorization")
                if auth_header and auth_header.lower().startswith("bearer "):
                    token = auth_header[7:].strip()
                if not token:
                    token = headers.get("x-profile-token")
                if not token:
                    query_params = QueryParams(scope.get("query_string", b"").decode("utf-8"))
                    token = query_params.get("token")

                profile = self._resolve_profile(token)

                is_messages = path.startswith("/messages")
                if is_messages:
                    query_params = QueryParams(scope.get("query_string", b"").decode("utf-8"))
                    session_id = query_params.get("session_id")
                    if profile is None and session_id:
                        # No (valid) token on this POST -- fall back to trusting an
                        # already-authenticated session, since the mcp SDK never
                        # gives clients a way to resend the token here.
                        profile = self._session_profiles.get(session_id)

                if profile is None:
                    body = b'{"error":{"code":"auth_required","message":"Invalid or missing MCP token"}}'
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("utf-8")),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body, "more_body": False})
                    return

                send_for_app = send
                new_session_ids: List[str] = []
                if path.startswith("/sse"):
                    # This is the GET /sse connection: watch its outgoing body for the
                    # SDK's "endpoint" event, which is the only place session_id is
                    # ever minted, and remember which profile authenticated it.
                    async def _capturing_send(message, _send=send, _profile=profile, _ids=new_session_ids):
                        if message.get("type") == "http.response.body":
                            match = re.search(rb"session_id=([0-9a-fA-F]{32})", message.get("body", b""))
                            if match:
                                sid = match.group(1).decode("ascii")
                                self._session_profiles[sid] = _profile
                                _ids.append(sid)
                        await _send(message)

                    send_for_app = _capturing_send

                token_t = current_profile.set(profile)
                try:
                    await self.app(scope, receive, send_for_app)
                    return
                finally:
                    current_profile.reset(token_t)
                    for sid in new_session_ids:
                        self._session_profiles.pop(sid, None)

        await self.app(scope, receive, send)

    def _resolve_profile(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        try:
            if appdb.DEFAULT_APP_DB_PATH.exists():
                with appdb.get_conn() as conn:
                    row = mcp_tokens_store.resolve_token(conn, token)
                    if row is not None:
                        mcp_tokens_store.touch_last_used(conn, row["id"])
                        user = users_store.get_user(conn, row["user_id"])
                        if user is not None and user["is_active"]:
                            return user["username"]
        except Exception:
            logger.exception("DB-backed MCP token lookup failed; falling back to .env token map")

        # Legacy fallback: reload the .env-scraped token map fresh each time,
        # exactly as the middleware it replaces did.
        self.token_map = load_token_profile_map()
        return self.token_map.get(token)


def build_http_app():
    """Builds the combined Starlette app served under SSE transport mode: the
    legacy /sse + /messages/ endpoints plus Streamable HTTP at /mcp, both
    backed by the same underlying MCP server session loop, so either kind of
    client can connect without any config change on our end. Does not attach
    AppAuthMiddleware -- callers add that (with whatever token_map they have)
    themselves, same as they add any other middleware.

    Note: `mcp` is a process-wide singleton, and the StreamableHTTPSessionManager
    it lazily creates on first call to streamable_http_app() can only have its
    run() lifespan entered once per instance (the SDK raises RuntimeError on a
    second attempt) -- fine for a real server process (this is called exactly
    once), but tests that call this more than once must reset
    `mcp._session_manager = None` first to get a fresh one."""
    app = mcp.sse_app()

    # streamable_http_app() builds its own Starlette app (and, as a side effect,
    # lazily creates mcp's StreamableHTTPSessionManager); we only need its /mcp
    # route. Inserted right after the native /sse + /messages/ routes (indices
    # 0-1) so it can't be shadowed by the SPA catch-all mounted among the custom
    # routes that follow.
    streamable_app = mcp.streamable_http_app()
    streamable_route = next(
        r for r in streamable_app.routes if getattr(r, "path", None) == mcp.settings.streamable_http_path
    )
    app.router.routes.insert(2, streamable_route)
    # StreamableHTTPSessionManager requires its run() context to be active for the
    # life of the app (it starts a task group used by every /mcp request) -- same
    # pattern FastMCP's own streamable_http_app() wires up internally.
    app.router.lifespan_context = lambda app: mcp.session_manager.run()

    return app


# Lazy initializers to ensure files are resolved within their active contexts
def get_resources(profile_name: str = "default"):
    # Override profile name with the one mapped from the SSE token context
    mapped_profile = current_profile.get("default")
    if mapped_profile != "default":
        profile_name = mapped_profile

    from config import Settings
    profile_settings = Settings.load_for_profile(profile_name)
    
    logger.debug("get_resources active. Profile: %s (mapped from context: %s). Paths: DB=%s, Token=%s, Creds=%s",
                profile_name, mapped_profile,
                profile_settings.workspace_dir / "email_cache.db",
                profile_settings.gmail_token_path,
                profile_settings.gmail_credentials_path)

    db = EmailDB(settings_instance=profile_settings)
    engine = EmailTriageEngine(db, settings_instance=profile_settings)
    return db, engine, profile_settings

def filter_emails_by_days(emails: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered = []
    for e in emails:
        d_str = e.get("date", "")
        if not d_str:
            filtered.append(e)
            continue
        try:
            dt = email.utils.parsedate_to_datetime(d_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                filtered.append(e)
        except Exception:
            filtered.append(e)
    return filtered

# =====================================================================
# BACKGROUND SYNC ENGINE (download + reconcile read-status + triage)
# =====================================================================

_sync_locks: Dict[str, threading.Lock] = {}
_sync_locks_guard = threading.Lock()

_stop_events: Dict[str, threading.Event] = {}
_stop_events_guard = threading.Lock()


def _get_profile_lock(profile: str) -> threading.Lock:
    with _sync_locks_guard:
        return _sync_locks.setdefault(profile, threading.Lock())


def _get_stop_event(profile: str) -> threading.Event:
    with _stop_events_guard:
        return _stop_events.setdefault(profile, threading.Event())


# Live progress of an in-flight sync_account call, keyed by account (e.g. settings.gmail_account).
# Present only while a sync is actively processing that account; absent once it finishes or errors.
_sync_progress: Dict[str, Dict[str, Any]] = {}
_sync_progress_guard = threading.Lock()


def _set_progress(account_label: str, **fields: Any) -> None:
    with _sync_progress_guard:
        _sync_progress.setdefault(account_label, {}).update(fields)


def _clear_progress(account_label: str) -> None:
    with _sync_progress_guard:
        _sync_progress.pop(account_label, None)


def _get_progress(account_label: str) -> Optional[Dict[str, Any]]:
    with _sync_progress_guard:
        entry = _sync_progress.get(account_label)
        return dict(entry) if entry is not None else None


def _run_tiered_triage(
    engine: Any, db: EmailDB, settings_instance: Any,
    msg_id: str, account: str, sender: str, subject: str, date_str: str, snippet: str, full_body: str,
) -> Dict[str, Any]:
    """
    Runs the VIP -> Level 0 -> Level 0.5 (rerank noise filter) -> Level 1 (+ premium escalation) -> Level 2
    tiered pipeline, mirroring the branch logic that used to live inline in fetch_and_process_unread's
    process_emails closure. Unlike that closure, full_body is always pre-supplied (already downloaded
    by sync_account) rather than lazily fetched.
    """
    if engine.is_vip_sender(sender):
        summary, _score, _l2_tag, l2_metrics = engine.run_level_2_summarization(subject, full_body)
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary,
            triage_level=2, tag="vip", email_body=full_body, level_1_run=False, level_2_run=True,
            level_2_prompt_tokens=l2_metrics["prompt_tokens"],
            level_2_completion_tokens=l2_metrics["completion_tokens"],
        )
        return {"triage_level": 2, "tag": "vip"}

    is_noise, l0_reason = engine.run_level_0_static(sender, subject)
    if is_noise:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="filtered", triage_level=0, tag="low", level_1_run=False, level_2_run=False
        )
        return {"triage_level": 0, "tag": "low"}

    tei_lvl, tei_reason, tei_score = engine.run_rerank_router(sender, subject, snippet)
    if tei_lvl == 0:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="tei_filtered", reason=tei_reason,
            score=tei_score, triage_level=0, tag="low", level_1_run=False, level_2_run=False
        )
        return {"triage_level": 0, "tag": "low"}

    suggested_lvl, reason, score, l1_tag, l1_metrics = engine.run_level_1_classification(sender, subject, snippet)

    if score < settings_instance.triage.confidence_threshold:
        suggested_lvl, reason, score, l1_tag = engine.run_level_1_premium_escalation(sender, subject, snippet, full_body)
        reason = f"[Premium Escalated] {reason}"

    if suggested_lvl == 0:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="downgraded", reason=reason, score=score,
            triage_level=0, tag=l1_tag, email_body=full_body, level_1_run=True, level_2_run=False,
            level_1_prompt_tokens=l1_metrics["prompt_tokens"],
            level_1_completion_tokens=l1_metrics["completion_tokens"],
        )
        return {"triage_level": 0, "tag": l1_tag}
    elif suggested_lvl == 1:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="unimportant", reason=reason, score=score,
            triage_level=1, tag=l1_tag, email_body=full_body, level_1_run=True, level_2_run=False,
            level_1_prompt_tokens=l1_metrics["prompt_tokens"],
            level_1_completion_tokens=l1_metrics["completion_tokens"],
        )
        return {"triage_level": 1, "tag": l1_tag}
    else:
        summary, sum_score, l2_tag, l2_metrics = engine.run_level_2_summarization(subject, full_body)
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary,
            reason=reason, score=sum_score, triage_level=2, tag=l2_tag,
            email_body=full_body, level_1_run=True, level_2_run=True,
            level_1_prompt_tokens=l1_metrics["prompt_tokens"],
            level_1_completion_tokens=l1_metrics["completion_tokens"],
            level_2_prompt_tokens=l2_metrics["prompt_tokens"],
            level_2_completion_tokens=l2_metrics["completion_tokens"],
        )
        return {"triage_level": 2, "tag": l2_tag, "summary": summary}


def _resolve_gmail_live_metadata(
    db: EmailDB, client: "GmailClient", account_label: str, id_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Resolves Gmail metadata (sender/subject/date/snippet/RFC Message-ID) for a bare list of
    {id, threadId} entries returned by messages.list, reusing whatever metadata we already
    cached for ids seen on a prior sync tick instead of re-fetching the entire live-unread set
    from the Gmail API every tick. For an account with a large persistent backlog that set can
    be in the thousands, and re-fetching it in full every scheduler interval is what trips
    Gmail's per-user rate limit.
    """
    if not id_entries:
        return []

    all_ids = [str(e["id"]) for e in id_entries]
    known = db.get_known_source_metadata(account_label, all_ids)

    new_entries = [e for e in id_entries if str(e["id"]) not in known]
    fetched = client._fetch_metadata_batch(new_entries) if new_entries else []
    fetched_by_id = {str(f["id"]): f for f in fetched}

    live: List[Dict[str, Any]] = []
    for e in id_entries:
        sid = str(e["id"])
        if sid in known:
            row = known[sid]
            live.append({
                "id": sid,
                "message_id": row["message_id"],
                "sender": row["sender"],
                "subject": row["subject"],
                "date": row["date_str"],
                "snippet": row["snippet"],
                "account": account_label,
            })
        elif sid in fetched_by_id:
            live.append(fetched_by_id[sid])
    return live


def sync_account(
    db: EmailDB, engine: Any, settings_instance: Any, client: Any, account_label: str,
    max_results: Optional[int], days: Optional[int],
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """One download + reconcile + triage pass for a single Gmail/IMAP client."""
    from datetime import datetime, timezone

    if stop_event and stop_event.is_set():
        return {
            "account": account_label, "status": "stopped",
            "downloaded": 0, "reconciled_read": 0, "triaged": 0, "auto_marked_read": 0, "errors": [],
        }

    summary: Dict[str, Any] = {
        "account": account_label, "downloaded": 0, "reconciled_read": 0, "triaged": 0,
        "auto_marked_read": 0, "errors": [],
    }
    try:
        _set_progress(account_label, phase="listing", total=0, processed=0, current_subject=None)
        try:
            if isinstance(client, GmailClient):
                id_entries = client.list_unread_ids(max_results=None, days=days)
                live = _resolve_gmail_live_metadata(db, client, account_label, id_entries)
            else:
                live = client.fetch_unread_headers(max_results=None, days=days)
            live_ids = {e["message_id"] for e in live}

            # Reconcile: previously-cached-unread messages no longer present in the live unread set
            # have since been read (elsewhere, e.g. webmail) -> flip them to read.
            newly_read = db.get_unread_message_ids(account_label) - live_ids
            for mid in newly_read:
                db.upsert_email_metadata(message_id=mid, account=account_label, is_unread=False)
            summary["reconciled_read"] = len(newly_read)

            if max_results:
                # Prioritize messages not yet triaged so each tick makes forward progress through
                # the backlog, instead of always re-selecting the same top-N slice of the live set
                # (which would starve everything past position max_results forever).
                triaged_ids = db.get_triaged_message_ids(account_label)
                pending = [e for e in live if e["message_id"] not in triaged_ids]
                to_process = pending[:max_results]
            else:
                to_process = live

            # Phase 1: figure out which messages already have a cached body vs need a fresh fetch.
            cached_by_id: Dict[str, Optional[Dict[str, Any]]] = {}
            need_body_source_ids: List[str] = []
            for e in to_process:
                cached = db.get_cached_result(e["message_id"])
                cached_by_id[e["message_id"]] = cached
                if not cached or not cached.get("email_body"):
                    need_body_source_ids.append(str(e["id"]))

            # Phase 2: batch-fetch bodies for everything that needs one in as few round trips as
            # possible (Gmail HTTP batching / a single multi-UID IMAP FETCH), instead of one
            # request per message. This is a throughput optimization only -- it does not reduce
            # API quota usage, so retry-with-backoff still applies inside each client.
            fetched_bodies: Dict[str, str] = {}
            if need_body_source_ids and not (stop_event and stop_event.is_set()):
                _set_progress(
                    account_label, phase="downloading", total=len(need_body_source_ids),
                    processed=0, current_subject=None,
                )
                fetched_bodies = client.fetch_full_bodies_batch(need_body_source_ids)

            # Phase 3: persist + triage each message, still checked/interruptible per-message
            # since this is where the slow LLM calls happen.
            _set_progress(account_label, phase="triaging", total=len(to_process), processed=0, current_subject=None)
            for idx, e in enumerate(to_process):
                if stop_event and stop_event.is_set():
                    summary["status"] = "stopped"
                    break
                msg_id = e["message_id"]
                cached = cached_by_id.get(msg_id)
                if cached and cached.get("email_body"):
                    full_body = cached["email_body"]
                else:
                    full_body = fetched_bodies.get(str(e["id"]), "")
                db.upsert_email_metadata(
                    message_id=msg_id, account=account_label, sender=e.get("sender"), subject=e.get("subject"),
                    date_str=e.get("date"), snippet=e.get("snippet"), source_id=str(e.get("id")),
                    email_body=full_body, is_unread=True,
                )
                summary["downloaded"] += 1
                if not cached or cached.get("triage_level") is None:
                    _run_tiered_triage(
                        engine, db, settings_instance, msg_id, account_label, e.get("sender"), e.get("subject"),
                        e.get("date"), e.get("snippet"), full_body,
                    )
                    summary["triaged"] += 1
                _set_progress(account_label, processed=idx + 1, current_subject=e.get("subject"))

            # Phase 4: auto-mark-read (opt-in, default off; independently configured per triage
            # level) -- flip anything that's already been triaged AND shown to the user via
            # fetch_and_process_unread at least that level's `after_displays` times to read, both
            # on the mail server and in the local cache.
            if not (stop_event and stop_event.is_set()):
                amr = settings_instance.auto_mark_read
                thresholds = {
                    level: level_cfg.after_displays
                    for level, level_cfg in ((0, amr.level_0), (1, amr.level_1), (2, amr.level_2))
                    if level_cfg.enabled
                }
                if thresholds:
                    candidates = db.get_auto_mark_read_candidates(account_label, thresholds)
                    source_ids = [c["source_id"] for c in candidates if c.get("source_id")]
                    if source_ids:
                        if client.mark_as_read(source_ids):
                            for c in candidates:
                                if c.get("source_id"):
                                    db.upsert_email_metadata(
                                        message_id=c["message_id"], account=account_label, is_unread=False,
                                    )
                            summary["auto_marked_read"] = len(source_ids)
                        else:
                            summary["errors"].append("auto-mark-read: failed to mark messages read remotely")
        except Exception as ex:
            logger.error("sync_account failed for %s: %s", account_label, ex, exc_info=True)
            summary["errors"].append(str(ex))
    finally:
        _clear_progress(account_label)

    summary["last_download_at"] = datetime.now(timezone.utc).isoformat()
    db.save_sync_summary(account_label, summary)
    return summary


from config import PLACEHOLDER_GMAIL_ACCOUNT as _PLACEHOLDER_GMAIL_ACCOUNT
from config import PLACEHOLDER_IMAP_LOGIN as _PLACEHOLDER_IMAP_LOGIN


def _db_integration_accounts(
    profile: str, profile_settings: Any, *, for_triage: bool = False, for_archive: bool = False
) -> Optional[List["account_clients.AccountClient"]]:
    """None means "no data/app.db user exists for this profile -- use the
    original single-Gmail+single-IMAP construction below, unchanged" (this is
    the case for every profile until migrate_to_db.py has run). A (possibly
    empty) list means a real user row exists and account_clients owns
    resolving their accounts, including its own legacy-settings fallback if
    they happen to have zero integrations rows."""
    if not appdb.DEFAULT_APP_DB_PATH.exists():
        return None
    try:
        with appdb.get_conn() as conn:
            user_row = users_store.get_user_by_username(conn, profile)
            if user_row is None:
                return None
            return account_clients.clients_for_user(
                conn, user_row["id"], profile_settings, for_triage=for_triage, for_archive=for_archive
            )
    except Exception:
        logger.exception(
            "Failed to resolve DB-backed integrations for profile %s; falling back to the legacy path", profile
        )
        return None


def sync_profile(profile: str) -> Dict[str, Any]:
    """
    Runs sync_account for every one of a profile's accounts, guarded by a per-profile lock.

    Two resolution paths coexist: a profile with a data/app.db user (see
    _db_integration_accounts) loops sync_account over however many
    Gmail/Zoho/IMAP integrations that user has connected; a profile with no
    such user (every profile until migrate_to_db.py has run) falls back to
    exactly the original single-Gmail+single-IMAP construction. Each side of
    the legacy path is skipped (not attempted at all) if its identity is
    still at the uninitialized placeholder default -- notably,
    `list_profile_names()` always includes "default", so an unconfigured
    "default" profile directory would otherwise be synced (and fail loudly
    with auth errors) on every scheduler tick even though no one asked for it
    to exist.
    """
    lock = _get_profile_lock(profile)
    if not lock.acquire(blocking=False):
        return {"profile": profile, "status": "skipped", "reason": "sync already in progress"}
    stop_event = _get_stop_event(profile)
    try:
        db, engine, profile_settings = get_resources(profile)
        result: Dict[str, Any] = {"profile": profile, "status": "ok"}

        db_accounts = _db_integration_accounts(profile, profile_settings, for_triage=True)
        if db_accounts is not None:
            result["accounts"] = {}
            for ac in db_accounts:
                if stop_event.is_set():
                    break
                try:
                    summary = sync_account(
                        db, engine, profile_settings, ac.client, ac.account,
                        profile_settings.scheduler.max_per_account, profile_settings.scheduler.days,
                        stop_event=stop_event,
                    )
                except Exception as e:
                    logger.error("Sync failed for %s account %s: %s", profile, ac.account, e, exc_info=True)
                    summary = {"errors": [str(e)]}
                result["accounts"][ac.account] = summary
                # Backward-compat keys for the pre-SPA dashboard, which only knows how to
                # render one gmail card and one imap/zoho card per profile.
                if ac.provider == "gmail" and "gmail" not in result:
                    result["gmail"] = summary
                elif ac.provider in ("imap", "zoho") and "imap" not in result:
                    result["imap"] = summary
            result.setdefault("gmail", {"status": "skipped", "reason": "gmail_account not configured"})
            result.setdefault("imap", {"status": "skipped", "reason": "imap_login not configured"})
        else:
            if not stop_event.is_set():
                if profile_settings.gmail_account == _PLACEHOLDER_GMAIL_ACCOUNT:
                    result["gmail"] = {"status": "skipped", "reason": "gmail_account not configured"}
                else:
                    try:
                        gmail = GmailClient(settings_instance=profile_settings)
                        result["gmail"] = sync_account(
                            db, engine, profile_settings, gmail, profile_settings.gmail_account,
                            profile_settings.scheduler.max_per_account, profile_settings.scheduler.days,
                            stop_event=stop_event,
                        )
                    except Exception as e:
                        logger.error("Gmail sync failed for profile %s: %s", profile, e, exc_info=True)
                        result["gmail"] = {"errors": [str(e)]}

            if not stop_event.is_set():
                if profile_settings.imap_login == _PLACEHOLDER_IMAP_LOGIN:
                    result["imap"] = {"status": "skipped", "reason": "imap_login not configured"}
                else:
                    try:
                        imap = IMAPClient(settings_instance=profile_settings)
                        result["imap"] = sync_account(
                            db, engine, profile_settings, imap, profile_settings.imap_login,
                            profile_settings.scheduler.max_per_account, profile_settings.scheduler.days,
                            stop_event=stop_event,
                        )
                    except Exception as e:
                        logger.error("IMAP sync failed for profile %s: %s", profile, e, exc_info=True)
                        result["imap"] = {"errors": [str(e)]}

        if stop_event.is_set():
            result["status"] = "stopped"
        return result
    finally:
        stop_event.clear()
        lock.release()


def sync_all_profiles() -> Dict[str, Any]:
    """Runs sync_profile for every configured profile under profiles/."""
    return {"profiles": {name: sync_profile(name) for name in list_profile_names()}}


# =====================================================================
# FULL MAILBOX ARCHIVE DOWNLOADER (manual, one-time/resumable: every message, no triage)
# =====================================================================
#
# Step 1 of a planned local full-archive + embedding search feature: download every message in
# the mailbox (not just unread) and cache its body, WITHOUT running it through the triage
# pipeline -- triage_level stays NULL, exactly like a downloaded-but-not-yet-triaged row from the
# regular sync engine. Shares the regular sync's per-profile lock/stop-event so the two can never
# run concurrently against the same profile's DB, and the existing Stop button also cancels this.

_full_download_progress: Dict[str, Dict[str, Any]] = {}
_full_download_progress_guard = threading.Lock()


def _set_full_download_progress(account_label: str, **fields: Any) -> None:
    with _full_download_progress_guard:
        _full_download_progress.setdefault(account_label, {}).update(fields)


def _clear_full_download_progress(account_label: str) -> None:
    with _full_download_progress_guard:
        _full_download_progress.pop(account_label, None)


def _get_full_download_progress(account_label: str) -> Optional[Dict[str, Any]]:
    with _full_download_progress_guard:
        entry = _full_download_progress.get(account_label)
        return dict(entry) if entry is not None else None


def _full_download_summary_key(account_label: str) -> str:
    """A distinct sync_state key so the archive summary doesn't clobber the regular sync's
    last-download summary -- both are stored in the same generic (account -> JSON) table."""
    return f"{account_label}::full_archive"


def full_download_account(db: EmailDB, client: Any, account_label: str, stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    """
    One-time (resumable) full-mailbox download: lists EVERY message on the server -- not scoped
    to unread -- and persists metadata + body via upsert_email_metadata. Never calls the triage
    pipeline, so triage_level stays NULL for anything not already triaged by the regular unread
    sync. Messages that already have a cached body are skipped entirely (no metadata or body
    re-fetch), so re-running this (or resuming after a stop) is cheap.
    """
    summary: Dict[str, Any] = {
        "account": account_label, "total_on_server": 0, "downloaded": 0, "skipped_cached": 0, "errors": [],
    }
    if stop_event and stop_event.is_set():
        summary["status"] = "stopped"
        return summary

    try:
        _set_full_download_progress(account_label, phase="listing_all", total=0, processed=0)
        if isinstance(client, GmailClient):
            id_entries = client.list_all_ids()
            archived = db.get_archived_source_ids(account_label)
            summary["total_on_server"] = len(id_entries)
            pending_entries = [e for e in id_entries if str(e["id"]) not in archived]
            summary["skipped_cached"] = len(id_entries) - len(pending_entries)
            live = _resolve_gmail_live_metadata(db, client, account_label, pending_entries)
        else:
            all_headers = client.fetch_all_headers()
            archived = db.get_archived_source_ids(account_label)
            summary["total_on_server"] = len(all_headers)
            live = [e for e in all_headers if str(e["id"]) not in archived]
            summary["skipped_cached"] = len(all_headers) - len(live)

        _set_full_download_progress(account_label, phase="downloading", total=len(live), processed=0)

        chunk_size = 200
        for i in range(0, len(live), chunk_size):
            if stop_event and stop_event.is_set():
                summary["status"] = "stopped"
                break
            chunk = live[i:i + chunk_size]
            source_ids = [str(e["id"]) for e in chunk]
            bodies = client.fetch_full_bodies_batch(source_ids)
            for e in chunk:
                sid = str(e["id"])
                db.upsert_email_metadata(
                    message_id=e["message_id"], account=account_label, sender=e.get("sender"),
                    subject=e.get("subject"), date_str=e.get("date"), snippet=e.get("snippet"),
                    source_id=sid, email_body=bodies.get(sid, ""),
                )
                summary["downloaded"] += 1
            _set_full_download_progress(account_label, processed=min(i + chunk_size, len(live)))
    except Exception as ex:
        logger.error("full_download_account failed for %s: %s", account_label, ex, exc_info=True)
        summary["errors"].append(str(ex))
    finally:
        _clear_full_download_progress(account_label)

    from datetime import datetime, timezone
    summary["last_full_download_at"] = datetime.now(timezone.utc).isoformat()
    db.save_sync_summary(_full_download_summary_key(account_label), summary)
    return summary


def full_download_profile(profile: str) -> Dict[str, Any]:
    """Runs full_download_account for every one of a profile's accounts, guarded by the same
    per-profile lock/stop-event as sync_profile. Same DB-vs-legacy resolution as sync_profile --
    see _db_integration_accounts."""
    lock = _get_profile_lock(profile)
    if not lock.acquire(blocking=False):
        return {"profile": profile, "status": "skipped", "reason": "sync already in progress"}
    stop_event = _get_stop_event(profile)
    try:
        db, _, profile_settings = get_resources(profile)
        result: Dict[str, Any] = {"profile": profile, "status": "ok"}

        db_accounts = _db_integration_accounts(profile, profile_settings, for_archive=True)
        if db_accounts is not None:
            result["accounts"] = {}
            for ac in db_accounts:
                if stop_event.is_set():
                    break
                try:
                    summary = full_download_account(db, ac.client, ac.account, stop_event=stop_event)
                except Exception as e:
                    logger.error("Full download failed for %s account %s: %s", profile, ac.account, e, exc_info=True)
                    summary = {"errors": [str(e)]}
                result["accounts"][ac.account] = summary
                if ac.provider == "gmail" and "gmail" not in result:
                    result["gmail"] = summary
                elif ac.provider in ("imap", "zoho") and "imap" not in result:
                    result["imap"] = summary
            result.setdefault("gmail", {"status": "skipped", "reason": "gmail_account not configured"})
            result.setdefault("imap", {"status": "skipped", "reason": "imap_login not configured"})
        else:
            if not stop_event.is_set():
                if profile_settings.gmail_account == _PLACEHOLDER_GMAIL_ACCOUNT:
                    result["gmail"] = {"status": "skipped", "reason": "gmail_account not configured"}
                else:
                    try:
                        gmail = GmailClient(settings_instance=profile_settings)
                        result["gmail"] = full_download_account(db, gmail, profile_settings.gmail_account, stop_event=stop_event)
                    except Exception as e:
                        logger.error("Gmail full download failed for profile %s: %s", profile, e, exc_info=True)
                        result["gmail"] = {"errors": [str(e)]}

            if not stop_event.is_set():
                if profile_settings.imap_login == _PLACEHOLDER_IMAP_LOGIN:
                    result["imap"] = {"status": "skipped", "reason": "imap_login not configured"}
                else:
                    try:
                        imap = IMAPClient(settings_instance=profile_settings)
                        result["imap"] = full_download_account(db, imap, profile_settings.imap_login, stop_event=stop_event)
                    except Exception as e:
                        logger.error("IMAP full download failed for profile %s: %s", profile, e, exc_info=True)
                        result["imap"] = {"errors": [str(e)]}

        if stop_event.is_set():
            result["status"] = "stopped"
        return result
    finally:
        stop_event.clear()
        lock.release()


def full_download_all_profiles() -> Dict[str, Any]:
    """Runs full_download_profile for every configured profile under profiles/."""
    return {"profiles": {name: full_download_profile(name) for name in list_profile_names()}}


def _start_full_download(profile: str) -> Dict[str, Any]:
    """Kicks off a full-mailbox download in a background thread and returns immediately."""
    if profile.strip().lower() == "all":
        threading.Thread(target=full_download_all_profiles, daemon=True).start()
    else:
        threading.Thread(target=lambda: full_download_profile(profile), daemon=True).start()
    return {"status": "started", "profile": profile}


def _is_configured(profile_settings: Any) -> bool:
    """True unless a profile's Gmail/IMAP identity is still at the uninitialized placeholder default."""
    return (
        profile_settings.gmail_account != _PLACEHOLDER_GMAIL_ACCOUNT
        or profile_settings.imap_login != _PLACEHOLDER_IMAP_LOGIN
    )


def _resolve_account_metadata(profile: str) -> Optional[List[Dict[str, Any]]]:
    """Cheap (no live client construction) counterpart to _db_integration_accounts,
    for status display. None means no data/app.db user exists for this profile --
    the caller should fall back to the legacy single-Gmail+single-IMAP display."""
    if not appdb.DEFAULT_APP_DB_PATH.exists():
        return None
    try:
        with appdb.get_conn() as conn:
            user_row = users_store.get_user_by_username(conn, profile)
            if user_row is None:
                return None
            rows = ints.list_integrations(conn, user_row["id"], enabled_only=True)
            return [
                {
                    "integration_id": r["id"], "provider": r["provider"], "account": r["cache_account_key"],
                    "label": r["account_label"] or r["cache_account_key"],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to read integrations metadata for profile %s status", profile)
        return None


def _profile_status(name: str) -> Dict[str, Any]:
    """Current sync status + last-download summary + cached counts for one profile's accounts."""
    db, _, profile_settings = get_resources(name)

    def _account_entry(account: str) -> Dict[str, Any]:
        return {
            "account": account,
            "summary": db.get_sync_summary(account),
            "counts": db.get_email_counts(account),
            "progress": _get_progress(account),
            "full_download_summary": db.get_sync_summary(_full_download_summary_key(account)),
            "full_download_progress": _get_full_download_progress(account),
        }

    status: Dict[str, Any] = {
        "profile": name,
        "configured": _is_configured(profile_settings),
        "running": _get_profile_lock(name).locked(),
        "stop_requested": _get_stop_event(name).is_set(),
        "gmail": _account_entry(profile_settings.gmail_account),
        "imap": _account_entry(profile_settings.imap_login),
    }

    accounts_meta = _resolve_account_metadata(name)
    if accounts_meta is not None:
        status["configured"] = bool(accounts_meta) or status["configured"]
        status["accounts"] = []
        gmail_set = imap_set = False
        for meta in accounts_meta:
            entry = {
                "integration_id": meta["integration_id"], "provider": meta["provider"], "label": meta["label"],
                **_account_entry(meta["account"]),
            }
            status["accounts"].append(entry)
            # Backward-compat: the pre-SPA dashboard only knows how to render one
            # gmail card and one imap/zoho card per profile.
            if meta["provider"] == "gmail" and not gmail_set:
                status["gmail"] = entry
                gmail_set = True
            elif meta["provider"] in ("imap", "zoho") and not imap_set:
                status["imap"] = entry
                imap_set = True

    return status


def _mask_secret(value: Any) -> str:
    """Renders a secret as a presence indicator only, never the value itself."""
    return "•••• (set)" if value else "(not set)"


def _profile_config(name: str) -> Dict[str, Any]:
    """Current effective (non-secret) config for one profile, for display on the dashboard."""
    _, _, s = get_resources(name)
    return {
        "gmail_account": s.gmail_account,
        "imap_host": s.imap_host,
        "imap_port": s.imap_port,
        "imap_login": s.imap_login,
        "imap_password": _mask_secret(s.imap_password),
        "smtp_host": s.smtp_host,
        "smtp_port": s.smtp_port,
        "smtp_login": s.active_smtp_login,
        "smtp_password": _mask_secret(s.active_smtp_password),
        "triage_base_url": s.triage_base_url,
        "triage_model": s.triage_model,
        "triage_api_key": _mask_secret(s.triage_api_key),
        "summary_base_url": s.summary_base_url,
        "summary_model": s.summary_model,
        "summary_api_key": _mask_secret(s.summary_api_key),
        "confidence_threshold": s.triage.confidence_threshold,
        "triage_type": s.triage.triage_type,
        "tei_url": s.triage.tei_url,
        "tei_model": s.triage.tei_model,
        "tei_api_key": _mask_secret(s.triage.tei_api_key),
        "tei_router_enabled": s.triage.tei_router_enabled,
        "tei_noise_enabled": s.triage.tei_noise_enabled,
        "tei_noise_threshold": s.triage.tei_noise_threshold,
        "whitelist_vip_senders": len(s.triage.whitelist_vip_senders),
        "whitelist_domains": len(s.triage.whitelist_domains),
        "blacklist_keywords": len(s.triage.blacklist_keywords),
        "blacklist_senders": len(s.triage.blacklist_senders),
        "scheduler_enabled": s.scheduler.enabled,
        "scheduler_interval": s.scheduler.interval,
        "scheduler_max_per_account": s.scheduler.max_per_account,
        "scheduler_days": s.scheduler.days,
        "auto_mark_read_level_0_enabled": s.auto_mark_read.level_0.enabled,
        "auto_mark_read_level_0_after_displays": s.auto_mark_read.level_0.after_displays,
        "auto_mark_read_level_1_enabled": s.auto_mark_read.level_1.enabled,
        "auto_mark_read_level_1_after_displays": s.auto_mark_read.level_1.after_displays,
        "auto_mark_read_level_2_enabled": s.auto_mark_read.level_2.enabled,
        "auto_mark_read_level_2_after_displays": s.auto_mark_read.level_2.after_displays,
    }


def _profile_token_stats(name: str, days: int = 30) -> Dict[str, Any]:
    """
    Daily input/output token usage for one profile over the last `days` days, plus tokens saved
    by the Level 0.5 rerank noise filter. tei_saved_tokens is forced to 0 for every day when the
    profile's current config has tei_router_enabled=False, regardless of what historical rows suggest.
    """
    db, _, profile_settings = get_resources(name)
    tei_enabled = bool(profile_settings.triage.tei_router_enabled)
    daily = db.get_daily_token_stats(days=days)
    if not tei_enabled:
        for entry in daily:
            entry["tei_saved_tokens"] = 0
    return {"tei_enabled": tei_enabled, "daily": daily}


def _combine_token_stats(per_profile: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sums each profile's 30-day daily token series into one cross-profile series for the dashboard."""
    combined_by_day: Dict[str, Dict[str, int]] = {}
    tei_enabled_any = False
    for stats in per_profile:
        tei_enabled_any = tei_enabled_any or stats.get("tei_enabled", False)
        for entry in stats.get("daily", []):
            agg = combined_by_day.setdefault(
                entry["day"], {"input_tokens": 0, "output_tokens": 0, "tei_saved_tokens": 0}
            )
            agg["input_tokens"] += entry["input_tokens"]
            agg["output_tokens"] += entry["output_tokens"]
            agg["tei_saved_tokens"] += entry["tei_saved_tokens"]
    return {
        "tei_enabled": tei_enabled_any,
        "daily": [{"day": day, **vals} for day, vals in sorted(combined_by_day.items())],
    }


def _dashboard_status(profile_names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Status payload backing the /api/status route and the web dashboard.
    `profile_names=None` (the default, used by every existing caller) means
    every configured profile; the /api/status route passes a restricted list
    to scope a non-admin caller to their own profile."""
    profiles: Dict[str, Any] = {}
    token_stats_per_profile: List[Dict[str, Any]] = []
    for name in (profile_names if profile_names is not None else list_profile_names()):
        status = _profile_status(name)
        # The "default" profile always exists (list_profile_names() guarantees it) even when no
        # one has ever set it up -- don't clutter the dashboard with a placeholder-only card for it.
        # Named profiles are always shown, even mid-setup, since the user created them intentionally.
        if name == "default" and not status["configured"]:
            continue
        token_stats_per_profile.append(_profile_token_stats(name))
        profiles[name] = {
            **status,
            "config": _profile_config(name),
        }

    return {
        "scheduler": {
            "enabled": settings.scheduler.enabled,
            "interval": settings.scheduler.interval,
            "interval_seconds": settings.scheduler.interval_seconds,
        },
        "download_all_scheduler": {
            "enabled": settings.download_all_scheduler.enabled,
            "interval": settings.download_all_scheduler.interval,
            "interval_seconds": settings.download_all_scheduler.interval_seconds,
        },
        # Token spend is shown as one combined 30-day total on the dashboard, not per profile.
        "token_stats": _combine_token_stats(token_stats_per_profile),
        "profiles": profiles,
    }


def _start_sync(profile: str) -> Dict[str, Any]:
    """Kicks off a sync in a background thread and returns immediately (does not wait for it)."""
    if profile.strip().lower() == "all":
        threading.Thread(target=sync_all_profiles, daemon=True).start()
    else:
        threading.Thread(target=lambda: sync_profile(profile), daemon=True).start()
    return {"status": "started", "profile": profile}


def _stop_sync(profile: str) -> Dict[str, Any]:
    """Requests a cooperative stop of any in-progress sync for the given profile(s)."""
    names = list_profile_names() if profile.strip().lower() == "all" else [profile]
    for name in names:
        _get_stop_event(name).set()
    return {"status": "stop_requested", "profile": profile}


# Single process-wide lock (unlike sync's per-profile locks) since one quality-check
# tick already loops over every profile/account itself -- see run_quality_check_all_profiles.
_quality_check_lock = threading.Lock()


def _start_quality_check_now() -> Dict[str, Any]:
    """Kicks off a full quality-check pass (every enabled profile/account) in a
    background thread and returns immediately -- the admin "Run now" button."""
    if not _quality_check_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "a quality check is already in progress"}

    def _run():
        try:
            result = quality_check.run_quality_check_all_profiles(force=True)
            logger.info("Manual quality check run complete: %s", result)
        except Exception:
            logger.exception("Manual quality check run failed")
        finally:
            _quality_check_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


# =====================================================================
# TOOLS SECTION
# =====================================================================

@mcp.tool()
def mark_emails_as_read(
    level: Optional[int] = None,
    message_id: Optional[str] = None,
    all_emails: bool = False,
    profile: str = "default"
) -> Dict[str, Any]:
    """
    Marks unread emails in the mailboxes as read based on specified criteria.
    Only one of level, message_id, or all_emails=True should be provided.

    :param level: The cached triage level (0 = noise, 1 = unimportant, 2 = important) to mark read.
    :param message_id: The specific Message-ID (RFC 2822 header) or internal ID of the email to mark read.
    :param all_emails: If True, marks all currently unread emails as read.
    :param profile: The dynamic profile environment to load (default: "default").
    :return: A dictionary detailing execution results, counts of marked emails, and any errors.
    """
    db, engine, settings = get_resources(profile)
    return engine.mark_emails_read(
        level=level,
        message_id=message_id,
        all_emails=all_emails
    )



@mcp.tool()
def fetch_and_process_unread(max_per_source: int = 5, days: int = 7, profile: str = "default") -> str:
    """
    Returns triage details/summaries for currently-unread emails FROM THE LOCAL CACHE.

    CRITICAL: This tool no longer calls Gmail/IMAP live. The cache is kept fresh by a periodic
    background sync job (interval configured via `scheduler` settings), which downloads unread
    mail (including full body), reconciles read/unread status, and triages new mail. Use
    `trigger_download` to force an immediate refresh, and `get_last_download_time` to check how
    stale the cached results might be before trusting this output.

    :param max_per_source: Maximum number of cached unread items to return per account source.
    :param days: Only include unread emails received within this number of past days.
    :param profile: Dynamic profile environment to load (default: "default").
    :return: A formatted string summary of currently-unread, cached triage results.
    """
    db, engine, settings = get_resources(profile)
    stats = {
        "scanned": 0,
        "level_0_filtered": 0,
        "level_1_unimportant": 0,
        "important_identified": 0,
        "pending_triage": 0,
    }
    run_results: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    for account in (settings.gmail_account, settings.imap_login):
        rows = db.get_unread_emails(account=account)
        rows_for_filter = [{**r, "date": r.get("date_str", "")} for r in rows]
        rows_for_filter = filter_emails_by_days(rows_for_filter, days)[:max_per_source]
        for r in rows_for_filter:
            stats["scanned"] += 1
            if r.get("triage_level") is None:
                stats["pending_triage"] += 1
                pending.append(r)
                continue
            lvl = r["triage_level"]
            if lvl == 0:
                stats["level_0_filtered"] += 1
            elif lvl == 1:
                stats["level_1_unimportant"] += 1
            elif lvl == 2:
                stats["important_identified"] += 1
            run_results.append(r)

    # A message only counts as "shown" once it's actually rendered to the user below -- pending
    # (not-yet-triaged) rows don't count, since auto-mark-read also requires a completed triage.
    db.increment_display_count([r["message_id"] for r in run_results])

    # Render detailed textual overview for the agent
    lines = [
        "## Email Triage Execution Summary (from local cache)",
        f"- **Total Scanned**: {stats['scanned']}",
        f"- **Level 0 (Noise Filtered)**: {stats['level_0_filtered']}",
        f"- **Level 1 (Low Importance)**: {stats['level_1_unimportant']}",
        f"- **Level 2 (Premium Summarized & Flagged)**: {stats['important_identified']}",
        f"- **Pending Background Triage**: {stats['pending_triage']}",
        "\n### Unread Items:\n"
    ]

    for item in run_results:
        tag = (item.get("tag") or "untagged").upper()
        lines.append(
            f"- **[{tag}]** *{item.get('sender')}* - **{item.get('subject')}** (Level {item.get('triage_level')})"
        )
        if item.get("level_2_summary"):
            lines.append(f"  *Summary:* {item['level_2_summary']}")

    if pending:
        lines.append("\n### Pending Background Triage (downloaded, not yet classified):\n")
        for item in pending:
            lines.append(f"- *{item.get('sender')}* - **{item.get('subject')}**")

    return "\n".join(lines)


@mcp.tool()
def trigger_download(profile: str = "default") -> Dict[str, Any]:
    """
    Manually triggers an immediate mailbox sync: downloads currently-unread mail (including full
    body), reconciles previously-cached-unread messages that have since been read elsewhere, and
    triages anything not yet classified, caching the results. This normally happens automatically
    on the background scheduler's interval; use this tool to force a refresh right now.

    :param profile: A specific profile name (default: "default"), or "all" to sync every
                     configured profile under profiles/ sequentially.
    :return: A dictionary summarizing the sync per account (counts downloaded/reconciled/triaged),
             or a "skipped" status if a sync for that profile is already in progress.
    """
    if profile.strip().lower() == "all":
        return sync_all_profiles()
    return sync_profile(profile)


@mcp.tool()
def get_last_download_time(profile: str = "default") -> Dict[str, Any]:
    """
    Returns the last background/manual sync summary (timestamp, counts, errors) for each account
    in a profile, so callers can judge how fresh fetch_and_process_unread's cached results are.

    :param profile: A specific profile name (default: "default"), or "all" for every profile.
    :return: A dictionary of per-account last-sync summaries (or None if never synced).
    """
    if profile.strip().lower() == "all":
        return {"profiles": {name: _profile_status(name) for name in list_profile_names()}}
    return _profile_status(profile)



@mcp.tool()
def create_new_draft(to: str, subject: str, body: str, account_type: str = "gmail", profile: str = "default") -> Dict[str, Any]:
    """
    Creates a new draft email (Gmail or IMAP).

    :param to: The recipient's email address.
    :param subject: The subject of the email.
    :param body: The text body content of the email.
    :param account_type: Either "gmail" or "imap" (default: "gmail").
    :param profile: The dynamic profile environment to load (default: "default").
    :return: A dictionary containing the created draft metadata from Gmail or IMAP.
    """
    _, _, settings = get_resources(profile)
    if account_type.lower() == "imap":
        imap = IMAPClient(settings_instance=settings)
        return imap.create_draft(to=to, subject=subject, body=body)
    else:
        gmail = GmailClient(settings_instance=settings)
        return gmail.create_draft(to=to, subject=subject, body=body)

@mcp.tool()
def create_draft_reply(message_id: str, body: str, account_type: Optional[str] = None, profile: str = "default") -> Dict[str, Any]:
    """
    Creates a draft reply to an existing email (by internal Gmail ID/IMAP UID or global Message-ID).

    :param message_id: The specific Message-ID (RFC 2822 header), Gmail internal ID, or IMAP UID of the email to reply to.
    :param body: The reply text body content.
    :param account_type: Optional override. Either "gmail" or "imap". If not provided, it will auto-detect from the local triage database cache.
    :param profile: The dynamic profile environment to load (default: "default").
    :return: A dictionary containing the created draft metadata.
    """
    db, _, settings = get_resources(profile)
    
    # Auto-detect account type
    detected_type = "gmail"
    if account_type:
        detected_type = account_type.lower()
    else:
        cached = db.get_cached_result(message_id)
        if cached:
            account = cached.get("account", "")
            if account == settings.imap_login:
                detected_type = "imap"

    if detected_type == "imap":
        imap = IMAPClient(settings_instance=settings)
        return imap.create_reply_draft(message_id=message_id, body=body)
    else:
        gmail = GmailClient(settings_instance=settings)
        return gmail.create_reply_draft(message_id=message_id, body=body)

@mcp.tool()
def send_email_reply(message_id: str, body: str, account_type: Optional[str] = None, profile: str = "default") -> Dict[str, Any]:
    """
    Sends a reply directly to an existing email (by internal Gmail ID/IMAP UID or global Message-ID).

    :param message_id: The specific Message-ID (RFC 2822 header), Gmail internal ID, or IMAP UID of the email to reply to.
    :param body: The reply text body content.
    :param account_type: Optional override. Either "gmail" or "imap". If not provided, it will auto-detect from the local triage database cache.
    :param profile: The dynamic profile environment to load (default: "default").
    :return: A dictionary containing the sent message metadata.
    """
    db, _, settings = get_resources(profile)
    
    # Auto-detect account type
    detected_type = "gmail"
    if account_type:
        detected_type = account_type.lower()
    else:
        cached = db.get_cached_result(message_id)
        if cached:
            account = cached.get("account", "")
            if account == settings.imap_login:
                detected_type = "imap"

    if detected_type == "imap":
        imap = IMAPClient(settings_instance=settings)
        return imap.send_reply(message_id=message_id, body=body)
    else:
        gmail = GmailClient(settings_instance=settings)
        return gmail.send_reply(message_id=message_id, body=body)

@mcp.tool()
def fetch_full_email(message_id: str, account_type: Optional[str] = None, profile: str = "default") -> Dict[str, Any]:
    """
    Fetches the full headers and body of a single email (by internal Gmail ID/IMAP UID or global
    Message-ID). Serves the local cache at 0 token/network cost if the body was already downloaded;
    otherwise fetches live from Gmail/IMAP and caches the result for next time.

    :param message_id: The specific Message-ID (RFC 2822 header), Gmail internal ID, or IMAP UID of the email to fetch.
    :param account_type: Optional override. Either "gmail" or "imap". If not provided, it will auto-detect from the local triage database cache.
    :param profile: The dynamic profile environment to load (default: "default").
    :return: A dictionary with sender, subject, date, body, account, and any cached triage metadata.
    """
    db, _, settings = get_resources(profile)

    cached = db.get_cached_result(message_id)
    if cached and cached.get("email_body"):
        return {
            "id": message_id,
            "message_id": message_id,
            "sender": cached.get("sender"),
            "subject": cached.get("subject"),
            "date": cached.get("date_str"),
            "body": cached.get("email_body"),
            "account": cached.get("account"),
            "triage_level": cached.get("triage_level"),
            "tag": cached.get("tag"),
            "summary": cached.get("level_2_summary"),
            "source": "cache",
        }

    # Auto-detect account type
    detected_type = "gmail"
    if account_type:
        detected_type = account_type.lower()
    elif cached:
        account = cached.get("account", "")
        if account == settings.imap_login:
            detected_type = "imap"

    if detected_type == "imap":
        imap = IMAPClient(settings_instance=settings)
        result = imap.fetch_full_email(message_id)
    else:
        gmail = GmailClient(settings_instance=settings)
        result = gmail.fetch_full_email(message_id)

    db.upsert_email_metadata(
        message_id=result["message_id"],
        account=result["account"],
        sender=result.get("sender"),
        subject=result.get("subject"),
        date_str=result.get("date"),
        email_body=result.get("body"),
    )
    result["source"] = "live"
    return result

@mcp.tool()
def search_emails(query: str, profile: str = "default") -> List[Dict[str, Any]]:
    """
    Searches the live Gmail and IMAP mailboxes for emails matching the query.
    Utilizes the internal cache to enrich search results with triage status, reason,
    scores, and executive summaries at 0 token cost.

    :param query: Search query text (e.g., "invoice", "urgent").
    :param profile: Dynamic profile environment to load (default: "default").
    :return: List of email records matching the query, enriched with internal cache details.
    """
    db, engine, settings = get_resources(profile)
    results = []
    
    # 1. Search Gmail
    try:
        gmail = GmailClient(settings_instance=settings)
        gmail_results = gmail.search_messages(query)
        for msg in gmail_results:
            msg_id = msg["message_id"]
            cached = db.get_cached_result(msg_id) or {}
            results.append({
                "id": msg["id"],
                "message_id": msg_id,
                "sender": msg["sender"],
                "subject": msg["subject"],
                "date": msg["date"],
                "snippet": msg["snippet"],
                "account": msg["account"],
                "triage_level": cached.get("triage_level"),
                "tag": cached.get("tag"),
                "reason": cached.get("reason") or ("Un-triaged" if not cached else "Cached"),
                "score": cached.get("score"),
                "summary": cached.get("level_2_summary")
            })
    except Exception as e:
        logger.error("Error searching Gmail inside MCP search tool: %s", e)

    # 2. Search IMAP
    try:
        imap = IMAPClient(settings_instance=settings)
        imap_results = imap.search_messages(query)
        for msg in imap_results:
            msg_id = msg["message_id"]
            cached = db.get_cached_result(msg_id) or {}
            results.append({
                "id": msg["id"],
                "message_id": msg_id,
                "sender": msg["sender"],
                "subject": msg["subject"],
                "date": msg["date"],
                "snippet": msg["snippet"],
                "account": msg["account"],
                "triage_level": cached.get("triage_level"),
                "tag": cached.get("tag"),
                "reason": cached.get("reason") or ("Un-triaged" if not cached else "Cached"),
                "score": cached.get("score"),
                "summary": cached.get("level_2_summary")
            })
    except Exception as e:
        logger.error("Error searching IMAP inside MCP search tool: %s", e)

    return results


def get_version_info() -> str:
    version_file = Path(__file__).parent.resolve() / "version.txt"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            return f"Error reading version.txt: {e}"
    return "unknown build: dev"


@mcp.custom_route("/version", methods=["GET"])
async def get_version(request: Request) -> PlainTextResponse:
    return PlainTextResponse(get_version_info())


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> Response:
    # Superseded by the SPA at "/" -- kept as a redirect so old bookmarks still land somewhere.
    return RedirectResponse(url="/", status_code=302)


@mcp.custom_route("/dashboard/logs", methods=["GET"])
async def dashboard_logs(request: Request) -> Response:
    return RedirectResponse(url="/logs", status_code=302)


@mcp.custom_route("/api/status", methods=["GET"])
@requires_active_user
async def api_status(request: Request) -> JSONResponse:
    identity: CurrentIdentity = request.state.identity
    show_all = identity.is_admin and request.query_params.get("all") == "1"
    names = None if show_all else [identity.username]
    return JSONResponse(_dashboard_status(names))


@mcp.custom_route("/api/sync/start", methods=["POST"])
@requires_active_user
async def api_sync_start(request: Request) -> Response:
    identity: CurrentIdentity = request.state.identity
    profile = request.query_params.get("profile", identity.username)
    if (profile.strip().lower() == "all" or profile != identity.username) and not identity.is_admin:
        return error_response(403, "forbidden", "Only admins may sync another user's profile or 'all'")
    return JSONResponse(_start_sync(profile))


@mcp.custom_route("/api/sync/stop", methods=["POST"])
@requires_active_user
async def api_sync_stop(request: Request) -> Response:
    identity: CurrentIdentity = request.state.identity
    profile = request.query_params.get("profile", identity.username)
    if (profile.strip().lower() == "all" or profile != identity.username) and not identity.is_admin:
        return error_response(403, "forbidden", "Only admins may stop another user's profile or 'all'")
    return JSONResponse(_stop_sync(profile))


@mcp.custom_route("/api/download_all/start", methods=["POST"])
@requires_active_user
async def api_download_all_start(request: Request) -> Response:
    identity: CurrentIdentity = request.state.identity
    profile = request.query_params.get("profile", identity.username)
    if (profile.strip().lower() == "all" or profile != identity.username) and not identity.is_admin:
        return error_response(403, "forbidden", "Only admins may download another user's profile or 'all'")
    return JSONResponse(_start_full_download(profile))


@mcp.custom_route("/api/quality/run-now", methods=["POST"])
@requires_admin
async def api_quality_run_now(request: Request) -> Response:
    return JSONResponse(_start_quality_check_now())


@mcp.custom_route("/api/logs", methods=["GET"])
@requires_admin
async def api_logs(request: Request) -> JSONResponse:
    since = int(request.query_params.get("since", 0))
    lines = _log_lines_since(since)
    return JSONResponse({"logs": lines, "last_seq": lines[-1]["seq"] if lines else since})


@mcp.custom_route("/api/logs/stream", methods=["GET"])
@requires_admin
async def api_logs_stream(request: Request) -> StreamingResponse:
    async def event_generator():
        last_seq = 0
        for entry in _log_lines_since(0):
            last_seq = entry["seq"]
            yield _sse_encode(entry["line"])
        while not await request.is_disconnected():
            await asyncio.sleep(1)
            for entry in _log_lines_since(last_seq):
                last_seq = entry["seq"]
                yield _sse_encode(entry["line"])

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# Registered last: FastMCP appends custom routes to the underlying Starlette app in
# registration order and gives them the lowest match precedence, so this catch-all can
# never shadow /sse, /messages/, or any /api/* route registered above.
web_static.register_spa_route(mcp)


if __name__ == "__main__":
    if settings.mcp_transport == "sse":
        import uvicorn
        import anyio

        # Bring up the app DB (users/sessions/mcp_tokens/integrations/settings)
        # and seed the bootstrap admin if this is a brand-new install. Both are
        # idempotent, so this is safe to run on every startup.
        appdb.init_app_db()
        with appdb.get_conn() as _bootstrap_conn:
            if users_store.seed_admin(_bootstrap_conn):
                logger.warning(
                    "Seeded bootstrap admin user with the default password -- it must be "
                    "changed at first login."
                )
            _seeded_prompts = prompts_store.seed_from_yaml_or_defaults(
                _bootstrap_conn, Path(__file__).parent.resolve() / "prompts.yml"
            )
            if _seeded_prompts:
                logger.info(
                    "Seeded %d prompt(s) into data/app.db from prompts.yml/hardcoded defaults "
                    "(admin-edited prompts, if any, were left untouched).",
                    _seeded_prompts,
                )

        # Load legacy profile token map (fallback for MCP tokens not yet migrated into the DB)
        token_map = load_token_profile_map()
        masked_map = {k[:4] + "...": v for k, v in token_map.items()}
        logger.info("Starting SSE MCP server. Loaded legacy profile token mappings: %s", masked_map)

        # Get the combined SSE (/sse, /messages/) + Streamable HTTP (/mcp) Starlette app
        app = build_http_app()

        # Add token validation middleware (DB-backed mcp_tokens, falling back to the legacy map)
        app.add_middleware(AppAuthMiddleware, token_map=token_map)
        
        async def run_server():
            config = uvicorn.Config(
                app,
                host=settings.mcp_host,
                port=settings.mcp_port,
                log_level=settings.log_level.lower(),
            )
            server = uvicorn.Server(config)
            await server.serve()

        async def scheduler_loop():
            interval = settings.scheduler.interval_seconds
            logger.info("Background sync scheduler enabled (interval: %ss)", interval)
            while True:
                try:
                    await anyio.to_thread.run_sync(sync_all_profiles)
                except Exception:
                    logger.exception("Background sync scheduler tick failed")
                await anyio.sleep(interval)

        async def quality_check_scheduler_loop():
            # Unlike scheduler_loop/download_all_scheduler_loop (fixed-interval), this fires once
            # a day at a configured wall-clock time -- sleep until the next occurrence of
            # settings.quality_check.hour:minute (today if still ahead, else tomorrow), run, repeat.
            from datetime import datetime, timedelta, timezone

            qc = settings.quality_check
            logger.info("Quality check scheduler enabled (daily at %02d:%02d UTC)", qc.hour, qc.minute)
            while True:
                now = datetime.now(timezone.utc)
                run_at = now.replace(hour=qc.hour, minute=qc.minute, second=0, microsecond=0)
                if run_at <= now:
                    run_at += timedelta(days=1)
                await anyio.sleep((run_at - now).total_seconds())
                try:
                    await anyio.to_thread.run_sync(quality_check.run_quality_check_all_profiles)
                except Exception:
                    logger.exception("Quality check scheduler tick failed")

        async def download_all_scheduler_loop():
            # Sleep first, then run -- unlike scheduler_loop, which fires immediately on startup
            # since a fresh unread-status refresh is cheap and useful right away. A full-mailbox
            # listing isn't: firing it on every container restart would be wasteful, so the first
            # run only happens once a full interval (nightly, by default) has actually elapsed.
            interval = settings.download_all_scheduler.interval_seconds
            logger.info("Full-mailbox download scheduler enabled (interval: %ss)", interval)
            while True:
                await anyio.sleep(interval)
                try:
                    await anyio.to_thread.run_sync(full_download_all_profiles)
                except Exception:
                    logger.exception("Full-mailbox download scheduler tick failed")

        async def run_all():
            async with anyio.create_task_group() as tg:
                if settings.scheduler.enabled:
                    tg.start_soon(scheduler_loop)
                else:
                    logger.info("Background sync scheduler disabled via config.")
                if settings.download_all_scheduler.enabled:
                    tg.start_soon(download_all_scheduler_loop)
                else:
                    logger.info("Full-mailbox download scheduler disabled via config.")
                if settings.quality_check.enabled:
                    tg.start_soon(quality_check_scheduler_loop)
                else:
                    logger.info("Quality check scheduler disabled via config.")
                # Run the server in the task group's own task (not start_soon) so that once it
                # returns (e.g. after SIGTERM/SIGINT triggers uvicorn's graceful shutdown), we can
                # explicitly wind down the scheduler loop too -- otherwise its `while True` never
                # exits on its own, the task group never completes, and the process hangs until
                # Docker's stop grace period elapses and force-kills it instead of exiting cleanly.
                await run_server()
                logger.info("Server shutting down; requesting any in-progress sync to stop...")
                _stop_sync("all")
                tg.cancel_scope.cancel()

        anyio.run(run_all)
    else:
        logger.info("Starting Stdio MCP server on stdin/stdout.")
        if settings.scheduler.enabled:
            logger.info("Background sync scheduler is only supported under SSE transport; skipping under stdio.")
        if settings.download_all_scheduler.enabled:
            logger.info("Full-mailbox download scheduler is only supported under SSE transport; skipping under stdio.")
        if settings.quality_check.enabled:
            logger.info("Quality check scheduler is only supported under SSE transport; skipping under stdio.")
        mcp.run(transport=settings.mcp_transport)
