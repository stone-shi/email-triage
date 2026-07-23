#!/usr/bin/env python3
"""
Model Context Protocol (MCP) Server for the Optimized Email Triage & Summarization Engine.
Exposes local database access, text search, and email triage pipelines to AI clients.
"""

import asyncio
import logging
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

import contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, StreamingResponse
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
    Runs the VIP -> Level 0 -> Level 0.5 (TEI router) -> Level 1 (+ premium escalation) -> Level 2
    tiered pipeline, mirroring the branch logic that used to live inline in fetch_and_process_unread's
    process_emails closure. Unlike that closure, full_body is always pre-supplied (already downloaded
    by sync_account) rather than lazily fetched.
    """
    if engine.is_vip_sender(sender):
        summary, _score, _l2_tag, _ = engine.run_level_2_summarization(subject, full_body)
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary,
            triage_level=2, tag="vip", email_body=full_body, level_1_run=False, level_2_run=True
        )
        return {"triage_level": 2, "tag": "vip"}

    is_noise, l0_reason = engine.run_level_0_static(sender, subject)
    if is_noise:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="filtered", triage_level=0, tag="low", level_1_run=False, level_2_run=False
        )
        return {"triage_level": 0, "tag": "low"}

    tei_lvl, tei_reason, tei_score = engine.run_tei_router(sender, subject, snippet)
    if tei_lvl == 0:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="tei_filtered", reason=tei_reason,
            score=tei_score, triage_level=0, tag="low", level_1_run=False, level_2_run=False
        )
        return {"triage_level": 0, "tag": "low"}
    elif tei_lvl == 2:
        summary, _score, l2_tag, _ = engine.run_level_2_summarization(subject, full_body)
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="tei_escalated", level_2_summary=summary,
            reason=tei_reason, score=tei_score, triage_level=2, tag=l2_tag,
            email_body=full_body, level_1_run=False, level_2_run=True
        )
        return {"triage_level": 2, "tag": l2_tag}

    suggested_lvl, reason, score, l1_tag, _l1_metrics = engine.run_level_1_classification(sender, subject, snippet)

    if score < settings_instance.triage.confidence_threshold:
        suggested_lvl, reason, score, l1_tag = engine.run_level_1_premium_escalation(sender, subject, snippet, full_body)
        reason = f"[Premium Escalated] {reason}"

    if suggested_lvl == 0:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="downgraded", reason=reason, score=score,
            triage_level=0, tag=l1_tag, email_body=full_body, level_1_run=True, level_2_run=False
        )
        return {"triage_level": 0, "tag": l1_tag}
    elif suggested_lvl == 1:
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="unimportant", reason=reason, score=score,
            triage_level=1, tag=l1_tag, email_body=full_body, level_1_run=True, level_2_run=False
        )
        return {"triage_level": 1, "tag": l1_tag}
    else:
        summary, sum_score, l2_tag, _ = engine.run_level_2_summarization(subject, full_body)
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary,
            reason=reason, score=sum_score, triage_level=2, tag=l2_tag,
            email_body=full_body, level_1_run=True, level_2_run=True
        )
        return {"triage_level": 2, "tag": l2_tag, "summary": summary}


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
            "downloaded": 0, "reconciled_read": 0, "triaged": 0, "errors": [],
        }

    summary: Dict[str, Any] = {
        "account": account_label, "downloaded": 0, "reconciled_read": 0, "triaged": 0, "errors": [],
    }
    try:
        _set_progress(account_label, phase="listing", total=0, processed=0, current_subject=None)
        try:
            if isinstance(client, GmailClient):
                live = client.fetch_unread_messages(max_results=None, days=days)
            else:
                live = client.fetch_unread_headers(max_results=None, days=days)
            live_ids = {e["message_id"] for e in live}

            # Reconcile: previously-cached-unread messages no longer present in the live unread set
            # have since been read (elsewhere, e.g. webmail) -> flip them to read.
            newly_read = db.get_unread_message_ids(account_label) - live_ids
            for mid in newly_read:
                db.upsert_email_metadata(message_id=mid, account=account_label, is_unread=False)
            summary["reconciled_read"] = len(newly_read)

            to_process = live[:max_results] if max_results else live

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
        except Exception as ex:
            logger.error("sync_account failed for %s: %s", account_label, ex, exc_info=True)
            summary["errors"].append(str(ex))
    finally:
        _clear_progress(account_label)

    summary["last_download_at"] = datetime.now(timezone.utc).isoformat()
    db.save_sync_summary(account_label, summary)
    return summary


def sync_profile(profile: str) -> Dict[str, Any]:
    """Runs sync_account for both Gmail and IMAP under one profile, guarded by a per-profile lock."""
    lock = _get_profile_lock(profile)
    if not lock.acquire(blocking=False):
        return {"profile": profile, "status": "skipped", "reason": "sync already in progress"}
    stop_event = _get_stop_event(profile)
    try:
        db, engine, profile_settings = get_resources(profile)
        result: Dict[str, Any] = {"profile": profile, "status": "ok"}

        if not stop_event.is_set():
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


_PLACEHOLDER_GMAIL_ACCOUNT = "your_email@gmail.com"
_PLACEHOLDER_IMAP_LOGIN = "your_email@domain.com"


def _is_configured(profile_settings: Any) -> bool:
    """True unless a profile's Gmail/IMAP identity is still at the uninitialized placeholder default."""
    return (
        profile_settings.gmail_account != _PLACEHOLDER_GMAIL_ACCOUNT
        or profile_settings.imap_login != _PLACEHOLDER_IMAP_LOGIN
    )


def _profile_status(name: str) -> Dict[str, Any]:
    """Current sync status + last-download summary + cached counts for one profile's accounts."""
    db, _, profile_settings = get_resources(name)
    return {
        "profile": name,
        "configured": _is_configured(profile_settings),
        "running": _get_profile_lock(name).locked(),
        "stop_requested": _get_stop_event(name).is_set(),
        "gmail": {
            "account": profile_settings.gmail_account,
            "summary": db.get_sync_summary(profile_settings.gmail_account),
            "counts": db.get_email_counts(profile_settings.gmail_account),
            "progress": _get_progress(profile_settings.gmail_account),
        },
        "imap": {
            "account": profile_settings.imap_login,
            "summary": db.get_sync_summary(profile_settings.imap_login),
            "counts": db.get_email_counts(profile_settings.imap_login),
            "progress": _get_progress(profile_settings.imap_login),
        },
    }


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
        "tei_signal_enabled": s.triage.tei_signal_enabled,
        "tei_noise_threshold": s.triage.tei_noise_threshold,
        "tei_signal_threshold": s.triage.tei_signal_threshold,
        "whitelist_vip_senders": len(s.triage.whitelist_vip_senders),
        "whitelist_domains": len(s.triage.whitelist_domains),
        "blacklist_keywords": len(s.triage.blacklist_keywords),
        "blacklist_senders": len(s.triage.blacklist_senders),
        "scheduler_enabled": s.scheduler.enabled,
        "scheduler_interval": s.scheduler.interval,
        "scheduler_max_per_account": s.scheduler.max_per_account,
        "scheduler_days": s.scheduler.days,
    }


def _dashboard_status() -> Dict[str, Any]:
    """Status payload backing the /api/status route and the web dashboard."""
    profiles: Dict[str, Any] = {}
    for name in list_profile_names():
        status = _profile_status(name)
        # The "default" profile always exists (list_profile_names() guarantees it) even when no
        # one has ever set it up -- don't clutter the dashboard with a placeholder-only card for it.
        # Named profiles are always shown, even mid-setup, since the user created them intentionally.
        if name == "default" and not status["configured"]:
            continue
        profiles[name] = {**status, "config": _profile_config(name)}

    return {
        "scheduler": {
            "enabled": settings.scheduler.enabled,
            "interval": settings.scheduler.interval,
            "interval_seconds": settings.scheduler.interval_seconds,
        },
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


# =====================================================================
# WEB DASHBOARD (status + manual sync controls)
# =====================================================================

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Email Triage - Sync Dashboard</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #f5f6f8; color: #1f2328; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .scheduler-info { color: #57606a; margin-bottom: 20px; font-size: 13px; }
  .global-actions { margin-bottom: 20px; }
  button { background: #2563eb; color: white; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; margin-right: 8px; }
  button.stop { background: #dc2626; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .card { background: #ffffff; border: 1px solid #d0d7de; border-radius: 10px; padding: 16px; box-shadow: 0 1px 2px rgba(31, 35, 40, 0.06); }
  .card h2 { margin: 0 0 8px; font-size: 15px; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; margin-left: 8px; }
  .badge.running { background: #dbeafe; color: #1d4ed8; }
  .badge.idle { background: #eef0f2; color: #57606a; }
  .account { border-top: 1px solid #e5e7eb; padding-top: 8px; margin-top: 8px; font-size: 13px; }
  .account .label { color: #57606a; }
  .errors { color: #b91c1c; }
  details.config { margin-top: 12px; }
  details.config summary { cursor: pointer; font-size: 12px; color: #2563eb; }
  .cfg-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 4px 12px; font-size: 12px; margin-top: 8px; }
  .cfg-key { color: #57606a; }
  .cfg-val { word-break: break-word; }
  .counts { color: #57606a; }
  .progress-wrap { margin-top: 6px; }
  .progress-bar { background: #e5e7eb; border-radius: 999px; height: 6px; overflow: hidden; }
  .progress-fill { background: #2563eb; height: 100%; }
  .progress-label { font-size: 11px; color: #57606a; margin-top: 3px; }
  .logs-section { margin-top: 28px; }
  .logs-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .logs-header h2 { font-size: 15px; margin: 0; }
  .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; display: inline-block; }
  .live-dot.disconnected { background: #d0d7de; }
  #log-console { background: #0f1115; color: #d1d5db; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; padding: 12px; border-radius: 8px; height: 260px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }
  #log-console .lvl-ERROR { color: #f87171; }
  #log-console .lvl-WARNING { color: #fbbf24; }
  #log-console .lvl-INFO { color: #d1d5db; }
</style>
</head>
<body>
  <h1>Email Triage &mdash; Sync Dashboard</h1>
  <div class="scheduler-info" id="scheduler-info">Loading...</div>
  <div class="global-actions">
    <button onclick="startSync('all')">Sync All</button>
    <button class="stop" onclick="stopSync('all')">Stop All</button>
  </div>
  <div class="grid" id="profiles"></div>

  <div class="logs-section">
    <div class="logs-header">
      <h2>Live Logs</h2>
      <span class="live-dot" id="log-live-dot"></span>
      <button onclick="document.getElementById('log-console').textContent = ''">Clear</button>
    </div>
    <div id="log-console"></div>
  </div>

<script>
async function loadStatus() {
  const res = await fetch('/api/status');
  const data = await res.json();
  const sched = data.scheduler;
  document.getElementById('scheduler-info').textContent =
    'Background scheduler: ' + (sched.enabled ? 'enabled' : 'disabled') + ' (interval: ' + sched.interval + ')';

  const container = document.getElementById('profiles');
  container.innerHTML = '';
  for (const [name, p] of Object.entries(data.profiles)) {
    container.appendChild(renderProfileCard(name, p));
  }
}

function renderCounts(counts) {
  if (!counts) return '';
  return '<div class="counts">total cached: ' + counts.total
    + ' &middot; L0: ' + counts.level_0
    + ' &middot; L1: ' + counts.level_1
    + ' &middot; L2: ' + counts.level_2
    + (counts.pending_triage ? ' &middot; pending: ' + counts.pending_triage : '')
    + '</div>';
}

function renderProgress(progress) {
  if (!progress) return '';
  const total = progress.total || 0;
  const processed = progress.processed || 0;
  const pct = total ? Math.round((processed / total) * 100) : 0;
  const label = progress.phase === 'listing'
    ? 'listing unread mail&hellip;'
    : processed + ' / ' + total + (progress.current_subject ? ' &mdash; ' + progress.current_subject : '');
  return '<div class="progress-wrap">'
    + '<div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%"></div></div>'
    + '<div class="progress-label">' + label + '</div>'
    + '</div>';
}

function renderAccount(acct) {
  const s = acct.summary;
  const counts = renderCounts(acct.counts);
  const progress = renderProgress(acct.progress);
  if (!s) {
    return '<div class="account"><div class="label">' + acct.account + '</div>' + counts + '<div>never synced</div>' + progress + '</div>';
  }
  const errors = (s.errors && s.errors.length) ? '<div class="errors">' + s.errors.join('; ') + '</div>' : '';
  const status = s.status ? ' (' + s.status + ')' : '';
  return '<div class="account">'
    + '<div class="label">' + acct.account + status + '</div>'
    + counts
    + '<div>last sync: ' + (s.last_download_at || 'n/a') + '</div>'
    + '<div>downloaded: ' + (s.downloaded ?? 0) + ' &middot; reconciled: ' + (s.reconciled_read ?? 0) + ' &middot; triaged: ' + (s.triaged ?? 0) + '</div>'
    + errors
    + progress
    + '</div>';
}

function renderConfig(cfg) {
  if (!cfg) return '';
  const rows = Object.entries(cfg).map(([k, v]) =>
    '<div class="cfg-key">' + k + '</div><div class="cfg-val">' + v + '</div>'
  ).join('');
  return '<details class="config"><summary>Configuration</summary><div class="cfg-grid">' + rows + '</div></details>';
}

function renderProfileCard(name, p) {
  const div = document.createElement('div');
  div.className = 'card';
  const badge = p.running
    ? '<span class="badge running">running</span>'
    : '<span class="badge idle">idle</span>';
  div.innerHTML = '<h2>' + name + ' ' + badge + '</h2>'
    + renderAccount(p.gmail)
    + renderAccount(p.imap)
    + '<div style="margin-top:12px">'
    + '<button ' + (p.running ? 'disabled' : '') + ' onclick="startSync(\\'' + name + '\\')">Sync Now</button>'
    + '<button class="stop" ' + (p.running ? '' : 'disabled') + ' onclick="stopSync(\\'' + name + '\\')">Stop</button>'
    + '</div>'
    + renderConfig(p.config);
  return div;
}

async function startSync(profile) {
  await fetch('/api/sync/start?profile=' + encodeURIComponent(profile), { method: 'POST' });
  loadStatus();
}

async function stopSync(profile) {
  await fetch('/api/sync/stop?profile=' + encodeURIComponent(profile), { method: 'POST' });
  loadStatus();
}

loadStatus();
setInterval(loadStatus, 5000);

const MAX_LOG_LINES = 500;

function appendLogLine(line) {
  const console_ = document.getElementById('log-console');
  const match = line.match(/\\[(INFO|WARNING|ERROR|CRITICAL|DEBUG)\\]/);
  const cls = match ? 'lvl-' + match[1] : '';
  const atBottom = console_.scrollTop + console_.clientHeight >= console_.scrollHeight - 5;

  const row = document.createElement('div');
  if (cls) row.className = cls;
  row.textContent = line;
  console_.appendChild(row);

  while (console_.childNodes.length > MAX_LOG_LINES) {
    console_.removeChild(console_.firstChild);
  }
  if (atBottom) {
    console_.scrollTop = console_.scrollHeight;
  }
}

function connectLogStream() {
  const dot = document.getElementById('log-live-dot');
  const source = new EventSource('/api/logs/stream');
  source.onopen = () => dot.classList.remove('disconnected');
  source.onerror = () => dot.classList.add('disconnected');
  source.onmessage = (event) => appendLogLine(event.data);
}

connectLogStream();
</script>
</body>
</html>
"""


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


@mcp.custom_route("/api/status", methods=["GET"])
async def api_status(request: Request) -> JSONResponse:
    return JSONResponse(_dashboard_status())


@mcp.custom_route("/api/sync/start", methods=["POST"])
async def api_sync_start(request: Request) -> JSONResponse:
    profile = request.query_params.get("profile", "all")
    return JSONResponse(_start_sync(profile))


@mcp.custom_route("/api/sync/stop", methods=["POST"])
async def api_sync_stop(request: Request) -> JSONResponse:
    profile = request.query_params.get("profile", "all")
    return JSONResponse(_stop_sync(profile))


@mcp.custom_route("/api/logs", methods=["GET"])
async def api_logs(request: Request) -> JSONResponse:
    since = int(request.query_params.get("since", 0))
    lines = _log_lines_since(since)
    return JSONResponse({"logs": lines, "last_seq": lines[-1]["seq"] if lines else since})


@mcp.custom_route("/api/logs/stream", methods=["GET"])
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


if __name__ == "__main__":
    if settings.mcp_transport == "sse":
        import uvicorn
        import anyio
        
        # Load profile token map
        token_map = load_token_profile_map()
        masked_map = {k[:4] + "...": v for k, v in token_map.items()}
        logger.info("Starting SSE MCP server. Loaded profile token mappings: %s", masked_map)
        
        # Get the standard FastMCP SSE Starlette app
        app = mcp.sse_app()
        
        # Add token validation middleware
        app.add_middleware(MCPTokenAuthMiddleware, token_map=token_map)
        
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

        async def run_all():
            async with anyio.create_task_group() as tg:
                if settings.scheduler.enabled:
                    tg.start_soon(scheduler_loop)
                else:
                    logger.info("Background sync scheduler disabled via config.")
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
        mcp.run(transport=settings.mcp_transport)
