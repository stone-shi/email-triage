import logging
import sys
import json
import argparse
from typing import List, Dict, Any
from db import EmailDB
from gmail_client import GmailClient
from imap_client import IMAPClient
from triage import EmailTriageEngine
from notifier import EmailNotifier

logger = logging.getLogger("email_triage.main")

def process_account_emails(
    emails: List[Dict[str, Any]], 
    client_source: Any, 
    engine: EmailTriageEngine, 
    db: EmailDB,
    stats: Dict[str, int],
    run_results: List[Dict[str, Any]],
    human_mode: bool
) -> None:
    """Process emails through the cache and triage tiers, dynamically outputting human logs or silent JSON accumulation."""
    for email in emails:
        stats["total_scanned"] += 1
        msg_id = email["message_id"]
        subject = email["subject"]
        sender = email["sender"]
        date_str = email["date"]
        snippet = email["snippet"]
        account = email["account"]

        # 1. Cache Layer Check
        if db.is_processed(msg_id):
            if human_mode:
                logger.info("Cache match: Skipping already processed Message-ID: %s", msg_id)
            stats["cached_skipped"] += 1
            continue

        if human_mode:
            logger.info("Processing incoming email: '%s' from %s", subject, sender)

        # VIP Whitelist Override Layer -> Direct to Level 2
        from config import settings
        is_vip = False
        for vip in getattr(settings.triage, "whitelist_vip_senders", []):
            if vip.lower() in sender.lower():
                if human_mode:
                    logger.info("VIP hit: Sender '%s' is a whitelisted VIP. Bypassing Level 0 and Level 1 directly to Level 2!", sender)
                is_vip = True
                break
                
        if is_vip:
            # Skip Level 0 & 1, go straight to fetch body and Level 2 summary
            full_id = email["id"]
            full_body = client_source.fetch_full_body(full_id)
            summary, summary_score = engine.run_level_2_summarization(subject, full_body)
            
            db.save_triage_result(
                msg_id, account, sender, subject, date_str,
                level_0_status="passed", level_1_status="important", level_2_summary=summary
            )
            
            run_results.append({
                "triage_level": "Level 2 (VIP)",
                "message_id": msg_id,
                "account": account,
                "sender": sender,
                "subject": subject,
                "date": date_str,
                "reason": "VIP Sender Direct Escalation",
                "summary": summary,
                "score": summary_score
            })
            stats["important_identified"] += 1
            
            if human_mode:
                from notifier import EmailNotifier
                EmailNotifier.print_terminal_banner(subject, sender, "VIP Sender Direct Escalation", summary, summary_score)
            continue

        # 2. Level 0 Static Regex Filter
        is_noise, l0_reason = engine.run_level_0_static(sender, subject)
        if is_noise:
            db.save_triage_result(msg_id, account, sender, subject, date_str, level_0_status="filtered")
            if human_mode:
                EmailNotifier.print_level_0_hit(msg_id, account, subject, l0_reason)
            
            run_results.append({
                "triage_level": "Level 0",
                "message_id": msg_id,
                "account": account,
                "subject": subject,
                "reason": l0_reason
            })
            stats["level_0_filtered"] += 1
            continue

        db.save_triage_result(msg_id, account, sender, subject, date_str, level_0_status="passed")

        # 3. Level 1 LLM Binary Triage
        is_important, reason, score = engine.run_level_1_classification(sender, subject, snippet)
        
        # Ambiguity Escalation Layer
        from config import settings
        if score < settings.triage.confidence_threshold:
            if human_mode:
                logger.info("Low confidence score (%s) from fast triage model. Escalating email to premium model...", score)
            full_id = email["id"]
            full_body = client_source.fetch_full_body(full_id)
            is_important, reason, score = engine.run_level_1_premium_escalation(sender, subject, snippet, full_body)
            reason = f"[Premium Escalated] {reason}"
        
        if not is_important:
            db.save_triage_result(
                msg_id, account, sender, subject, date_str, 
                level_0_status="passed", level_1_status="unimportant"
            )
            if human_mode:
                EmailNotifier.print_level_1_hit(msg_id, account, subject, reason, score)
            
            run_results.append({
                "triage_level": "Level 1 (Escalated)" if "[Premium Escalated]" in reason else "Level 1",
                "message_id": msg_id,
                "account": account,
                "subject": subject,
                "reason": reason,
                "score": score
            })
            stats["level_1_unimportant"] += 1
            continue

        # 4. Level 2 Premium Summary (Only for Important Emails)
        stats["important_identified"] += 1
        
        # Fetch full body payload now that email has passed Level 1 triage
        full_id = email["id"]
        full_body = client_source.fetch_full_body(full_id)
        
        summary, summary_score = engine.run_level_2_summarization(subject, full_body)
        
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary
        )
        
        # 5. Real-time Notification Alerts (Only printed if human mode requested)
        if human_mode:
            EmailNotifier.print_terminal_banner(subject, sender, reason, summary, summary_score)
        
        run_results.append({
            "triage_level": "Level 2",
            "message_id": msg_id,
            "account": account,
            "sender": sender,
            "subject": subject,
            "date": date_str,
            "reason": reason,
            "summary": summary,
            "score": summary_score
        })

def main() -> None:
    # Handle command-line arguments
    parser = argparse.ArgumentParser(description="Optimized Email Triage & Summarization Engine")
    parser.add_argument("--human", action="store_true", help="Output human readable logs and layout headers")
    parser.add_argument("--pretty", action="store_true", help="Pretty print the final JSON result array")
    parser.add_argument("--auth", action="store_true", help="Force full interactive Gmail OAuth re-authorization flow")
    parser.add_argument("--headless", action="store_true", help="Run OAuth authentication flow in headless/SSH console input mode")
    args = parser.parse_args()

    # Populate global settings singleton fields from command line arguments
    from config import settings
    settings.headless_mode = args.headless

    # Configure logging based on human_mode flag
    if args.human:
        log_level_numeric = getattr(logging, settings.log_level, logging.INFO)
        logging.basicConfig(
            level=log_level_numeric,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        logger.info("Starting Optimized Email Triage & Summarization Engine (Human Mode)...")
    else:
        # Suppress all log outputs for pure raw JSON formatting output mode
        logging.basicConfig(level=logging.CRITICAL, handlers=[logging.NullHandler()])
        logging.getLogger().handlers = []
        # Turn down logger library levels
        logging.getLogger("email_triage").setLevel(logging.CRITICAL)

    db = EmailDB()
    engine = EmailTriageEngine(db)
    
    stats = {
        "total_scanned": 0,
        "cached_skipped": 0,
        "level_0_filtered": 0,
        "level_1_unimportant": 0,
        "important_identified": 0
    }
    
    run_results = []

    # --- Ingest Gmail ---
    try:
        if args.auth and settings.gmail_token_path.exists():
            if args.human:
                logger.info("Forcing re-authentication: purging persistent token file...")
            try:
                settings.gmail_token_path.unlink()
            except Exception:
                pass
                
        if args.human:
            logger.info("Initializing Gmail Client Layer...")
        gmail = GmailClient()
        gmail_emails = gmail.fetch_unread_messages()
        process_account_emails(gmail_emails, gmail, engine, db, stats, run_results, args.human)
    except Exception as e:
        if args.human:
            logger.error("Error during Gmail pipeline run: %s", e)

    # --- Ingest IMAP (Zoho) ---
    try:
        if args.human:
            logger.info("Initializing IMAP Client Layer...")
        imap = IMAPClient()
        imap_emails = imap.fetch_unread_headers()
        process_account_emails(imap_emails, imap, engine, db, stats, run_results, args.human)
    except Exception as e:
        if args.human:
            logger.error("Error during IMAP pipeline run: %s", e)

    # --- Output Run Content ---
    if args.human:
        border = "=" * 60
        print(f"\n{border}\n📊 ENGINE RUN METRICS AND TELEMETRY SUMMARY\n{border}")
        print(f" Total Email Envelopes Scanned: {stats['total_scanned']}")
        print(f" Cached Duplicates Skipped:      {stats['cached_skipped']}")
        print(f" Level 0 Static Noise Filtered:   {stats['level_0_filtered']}")
        print(f" Level 1 Marked Low Importance:  {stats['level_1_unimportant']}")
        print(f" Level 2 Summarized & Flagged:   {stats['important_identified']}")
        print(border)
    else:
        # Standard execution JSON output mode ONLY: emit pure raw valid JSON without any extra text lines
        if args.pretty:
            print(json.dumps(run_results, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(run_results, ensure_ascii=False))

if __name__ == "__main__":
    main()
