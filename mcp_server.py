#!/usr/bin/env python3
"""
Model Context Protocol (MCP) Server for the Optimized Email Triage & Summarization Engine.
Exposes local database access, text search, and email triage pipelines to AI clients.
"""

import logging
import sys
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
from config import settings

# Initialize FastMCP server
from mcp.server.transport_security import TransportSecuritySettings

security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP(
    "Email Triage Engine",
    host=settings.mcp_host,
    port=settings.mcp_port,
    transport_security=security
)

import contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

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
    if profiles_dir.exists():
        for p_path in profiles_dir.iterdir():
            if p_path.is_dir():
                profile_name = p_path.name
                profile_env = p_path / ".env"
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
# TOOLS SECTION
# =====================================================================

@mcp.tool()
def fetch_and_process_unread(max_per_source: int = 5, days: int = 7, profile: str = "default") -> str:
    """
    Triggers unread email ingestion from Gmail and IMAP, runs the multi-tier triage engine,
    saves details in local cache, and returns results.

    :param max_per_source: Maximum number of unread emails to process per account source.
    :param days: Retrieve only unread emails received within this number of past days.
    :param profile: Dynamic profile environment to load (default: "default").
    :return: A formatted string summary of the processed results.
    """
    db, engine, settings = get_resources(profile)
    stats = {
        "scanned": 0,
        "cached_skipped": 0,
        "level_0_filtered": 0,
        "level_1_unimportant": 0,
        "important_identified": 0
    }
    run_results = []

    def process_emails(emails_list: List[Dict[str, Any]], client_source: Any):
        for email in emails_list:
            stats["scanned"] += 1
            msg_id = email["message_id"]
            subject = email["subject"]
            sender = email["sender"]
            date_str = email["date"]
            snippet = email["snippet"]
            account = email["account"]

            # Cache Check
            cached_row = db.get_cached_result(msg_id)
            if cached_row:
                stats["cached_skipped"] += 1
                run_results.append({
                    "message_id": msg_id,
                    "subject": subject,
                    "sender": sender,
                    "triage_level": cached_row.get("triage_level"),
                    "tag": cached_row.get("tag"),
                    "reason": cached_row.get("reason") or "Cached Result",
                    "summary": cached_row.get("level_2_summary")
                })
                continue

            # VIP Sender Check
            if engine.is_vip_sender(sender):
                full_body = client_source.fetch_full_body(email["id"])
                summary, score, l2_tag, _ = engine.run_level_2_summarization(subject, full_body)
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="passed", level_1_status="important", level_2_summary=summary,
                    triage_level=2, tag="vip", email_body=full_body, level_1_run=False, level_2_run=True
                )
                stats["important_identified"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 2, "tag": "vip", "reason": "VIP Whitelist Bypass", "summary": summary
                })
                continue

            # Level 0 Static Regex Filter
            is_noise, l0_reason = engine.run_level_0_static(sender, subject)
            if is_noise:
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="filtered", triage_level=0, tag="low", level_1_run=False, level_2_run=False
                )
                stats["level_0_filtered"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 0, "tag": "low", "reason": l0_reason
                })
                continue

            # Level 0.5 TEI Router
            tei_lvl, tei_reason, tei_score = engine.run_tei_router(sender, subject, snippet)
            if tei_lvl == 0:
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="passed", level_1_status="tei_filtered", reason=tei_reason,
                    score=tei_score, triage_level=0, tag="low", level_1_run=False, level_2_run=False
                )
                stats["level_0_filtered"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 0, "tag": "low", "reason": tei_reason
                })
                continue
            elif tei_lvl == 2:
                full_body = client_source.fetch_full_body(email["id"])
                summary, score, l2_tag, _ = engine.run_level_2_summarization(subject, full_body)
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="passed", level_1_status="tei_escalated", level_2_summary=summary,
                    reason=tei_reason, score=tei_score, triage_level=2, tag=l2_tag,
                    email_body=full_body, level_1_run=False, level_2_run=True
                )
                stats["important_identified"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 2, "tag": l2_tag, "reason": tei_reason, "summary": summary
                })
                continue

            # Level 1 classification
            suggested_lvl, reason, score, l1_tag, l1_metrics = engine.run_level_1_classification(sender, subject, snippet)

            # Confidence escalation check
            full_body = None
            if score < settings.triage.confidence_threshold or suggested_lvl > 0:
                full_body = client_source.fetch_full_body(email["id"])
            
            if score < settings.triage.confidence_threshold:
                suggested_lvl, reason, score, l1_tag = engine.run_level_1_premium_escalation(sender, subject, snippet, full_body)
                reason = f"[Premium Escalated] {reason}"

            if suggested_lvl == 0:
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="passed", level_1_status="downgraded", reason=reason, score=score,
                    triage_level=0, tag=l1_tag, email_body=full_body, level_1_run=True, level_2_run=False
                )
                stats["level_0_filtered"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 0, "tag": l1_tag, "reason": reason
                })
            elif suggested_lvl == 1:
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="passed", level_1_status="unimportant", reason=reason, score=score,
                    triage_level=1, tag=l1_tag, email_body=full_body, level_1_run=True, level_2_run=False
                )
                stats["level_1_unimportant"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 1, "tag": l1_tag, "reason": reason
                })
            elif suggested_lvl == 2:
                summary, sum_score, l2_tag, _ = engine.run_level_2_summarization(subject, full_body)
                db.save_triage_result(
                    msg_id, account, sender, subject, date_str,
                    level_0_status="passed", level_1_status="important", level_2_summary=summary,
                    reason=reason, score=sum_score, triage_level=2, tag=l2_tag,
                    email_body=full_body, level_1_run=True, level_2_run=True
                )
                stats["important_identified"] += 1
                run_results.append({
                    "message_id": msg_id, "subject": subject, "sender": sender,
                    "triage_level": 2, "tag": l2_tag, "reason": reason, "summary": summary
                })

    # 1. Fetch Gmail unread emails
    try:
        gmail = GmailClient(settings_instance=settings)
        gmail_emails = gmail.fetch_unread_messages()
        gmail_emails = filter_emails_by_days(gmail_emails, days)[:max_per_source]
        process_emails(gmail_emails, gmail)
    except Exception as e:
        logger.error("Error processing Gmail inside MCP tool: %s", e)

    # 2. Fetch IMAP unread emails
    try:
        imap = IMAPClient(settings_instance=settings)
        imap_emails = imap.fetch_unread_headers()
        imap_emails = filter_emails_by_days(imap_emails, days)[:max_per_source]
        process_emails(imap_emails, imap)
    except Exception as e:
        logger.error("Error processing IMAP inside MCP tool: %s", e)

    # Render detailed textual overview for the agent
    lines = [
        "## Email Triage Execution Summary",
        f"- **Total Scanned**: {stats['scanned']}",
        f"- **Cached Duplicates (Skipped)**: {stats['cached_skipped']}",
        f"- **Level 0 (Noise Filtered)**: {stats['level_0_filtered']}",
        f"- **Level 1 (Low Importance)**: {stats['level_1_unimportant']}",
        f"- **Level 2 (Premium Summarized & Flagged)**: {stats['important_identified']}",
        "\n### Newly Triaged Items:\n"
    ]
    
    for item in run_results:
        lines.append(
            f"- **[{item['tag'].upper()}]** *{item['sender']}* - **{item['subject']}** (Level {item['triage_level']})"
        )
        if item.get("summary"):
            lines.append(f"  *Summary:* {item['summary']}")
            
    return "\n".join(lines)

@mcp.tool()
def list_cached_emails(limit: int = 20, triage_level: Optional[int] = None, profile: str = "default") -> List[Dict[str, Any]]:
    """
    Retrieves a list of recently cached triage email records from the local SQLite database.

    :param limit: The maximum number of records to return (default: 20).
    :param triage_level: Optional filter for triage level (0 = Noise, 1 = Unimportant, 2 = Important/Summary).
    :param profile: Dynamic profile environment to load (default: "default").
    :return: List of dictionaries containing metadata for matching emails.
    """
    db, _, _ = get_resources(profile)
    results = []
    try:
        import sqlite3
        with db._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if triage_level is not None:
                cursor.execute(
                    "SELECT message_id, account, sender, subject, date_str, triage_level, tag, reason, score, processed_at FROM email_cache WHERE triage_level = ? ORDER BY processed_at DESC LIMIT ?",
                    (triage_level, limit)
                )
            else:
                cursor.execute(
                    "SELECT message_id, account, sender, subject, date_str, triage_level, tag, reason, score, processed_at FROM email_cache ORDER BY processed_at DESC LIMIT ?",
                    (limit,)
                )
            results = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error("Error querying cached emails in list_cached_emails: %s", e)
    return results

@mcp.tool()
def get_email_details(message_id: str, profile: str = "default") -> Dict[str, Any]:
    """
    Fetches the detailed record of a cached email by its unique Message-ID.
    Returns metadata, full email body, pipeline reasons, confidence scores, and premium summaries.

    :param message_id: The exact Message-ID value of the target email.
    :param profile: Dynamic profile environment to load (default: "default").
    :return: Dict containing the database fields of the email, or an error dict if not found.
    """
    db, _, _ = get_resources(profile)
    cached = db.get_cached_result(message_id)
    if not cached:
        return {"error": f"No email found in local cache matching message_id: {message_id}"}
    return cached

@mcp.tool()
def triage_single_email(message_id: str, profile: str = "default") -> Dict[str, Any]:
    """
    Forces a re-evaluation/triage of a specific email already cached.
    This fetches the body, re-runs static routing, runs classification, and regenerates Level 2 summaries.

    :param message_id: The exact Message-ID value of the target email.
    :param profile: Dynamic profile environment to load (default: "default").
    :return: Dict containing the updated triage database record.
    """
    db, engine, settings = get_resources(profile)
    cached = db.get_cached_result(message_id)
    if not cached:
        return {"error": f"Email with message_id {message_id} must already exist in cache to re-triage."}
    
    subject = cached.get("subject") or ""
    sender = cached.get("sender") or ""
    snippet = cached.get("subject") or ""  # Fallback
    body = cached.get("email_body") or snippet
    account = cached.get("account") or "unknown"
    date_str = cached.get("date_str") or ""

    # Force full run
    is_noise, l0_reason = engine.run_level_0_static(sender, subject)
    if is_noise:
        db.save_triage_result(
            message_id, account, sender, subject, date_str,
            level_0_status="filtered", triage_level=0, tag="low", level_1_run=False, level_2_run=False
        )
        return db.get_cached_result(message_id)

    suggested_lvl, reason, score, l1_tag, _ = engine.run_level_1_classification(sender, subject, body[:300])
    if suggested_lvl == 2:
        summary, sum_score, l2_tag, _ = engine.run_level_2_summarization(subject, body)
        db.save_triage_result(
            message_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary,
            reason=reason, score=sum_score, triage_level=2, tag=l2_tag,
            email_body=body, level_1_run=True, level_2_run=True
        )
    else:
        db.save_triage_result(
            message_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="unimportant", reason=reason, score=score,
            triage_level=suggested_lvl, tag=l1_tag, email_body=body, level_1_run=True, level_2_run=False
        )

    return db.get_cached_result(message_id)

@mcp.tool()
def search_emails(query: str, profile: str = "default") -> List[Dict[str, Any]]:
    """
    Performs a text search on the database of processed emails.
    Checks sender, subject, and email body fields.

    :param query: Search query text (e.g., "invoice", "important meeting").
    :param profile: Dynamic profile environment to load (default: "default").
    :return: List of email records matching the search query.
    """
    db, _, _ = get_resources(profile)
    results = []
    try:
        import sqlite3
        with db._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            sql = """
                SELECT message_id, account, sender, subject, date_str, triage_level, tag, reason, score, processed_at 
                FROM email_cache 
                WHERE sender LIKE ? OR subject LIKE ? OR email_body LIKE ?
                ORDER BY processed_at DESC LIMIT 50
            """
            like_query = f"%{query}%"
            cursor.execute(sql, (like_query, like_query, like_query))
            results = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error("Error searching emails in SQLite: %s", e)
    return results

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
            
        anyio.run(run_server)
    else:
        logger.info("Starting Stdio MCP server on stdin/stdout.")
        mcp.run(transport=settings.mcp_transport)
