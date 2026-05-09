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
        cached_row = db.get_cached_result(msg_id)
        if cached_row:
            t_level = cached_row.get("triage_level", 0)
            c_reason = cached_row.get("reason") or "Cached result"
            c_score = cached_row.get("score", 0.0) if cached_row.get("score") is not None else 0.0
            c_summary = cached_row.get("level_2_summary")
            c_tag = cached_row.get("tag") or ("low" if t_level == 0 else "notification")
            
            if human_mode:
                logger.info("Cache match: Reusing existing triage results for Message-ID: %s (Level: %s)", msg_id, t_level)
                if t_level == 0:
                    EmailNotifier.print_level_0_hit(msg_id, account, subject, c_reason)
                elif t_level == 1:
                    EmailNotifier.print_level_1_hit(msg_id, account, subject, c_reason, c_score)
                elif t_level == 2:
                    EmailNotifier.print_terminal_banner(subject, sender, c_reason, c_summary or "", c_score)
            
            res_obj = {
                "triage_level": t_level,
                "message_id": msg_id,
                "account": account,
                "sender": sender,
                "subject": subject,
                "date": date_str,
                "reason": c_reason,
                "score": c_score,
                "tag": c_tag
            }
            if t_level == 2:
                res_obj["summary"] = c_summary
                
            run_results.append(res_obj)
            stats["cached_skipped"] += 1
            continue

        if human_mode:
            logger.info("Processing incoming email: '%s' from %s", subject, sender)

        # VIP Whitelist Override Layer -> Direct to Level 2
        if engine.is_vip_sender(sender):
            if human_mode:
                logger.info("VIP hit: Sender '%s' is a whitelisted VIP. Bypassing Level 0 and Level 1 directly to Level 2!", sender)
            # Skip Level 0 & 1, go straight to fetch body and Level 2 summary
            full_id = email["id"]
            full_body = client_source.fetch_full_body(full_id)
            summary, summary_score, l2_tag, l2_metrics = engine.run_level_2_summarization(subject, full_body)
            
            db.save_triage_result(
                msg_id, account, sender, subject, date_str,
                level_0_status="passed", level_1_status="important", level_2_summary=summary,
                triage_level=2, tag="vip"
            )
            
            run_results.append({
                "triage_level": 2,
                "message_id": msg_id,
                "account": account,
                "sender": sender,
                "subject": subject,
                "date": date_str,
                "reason": "VIP Sender Direct Escalation",
                "summary": summary,
                "score": summary_score,
                "tag": "vip"
            })
            stats["important_identified"] += 1
            
            if human_mode:
                EmailNotifier.print_terminal_banner(subject, sender, "VIP Sender Direct Escalation", summary, summary_score)
            continue

        # 2. Level 0 Static Regex Filter
        is_noise, l0_reason = engine.run_level_0_static(sender, subject)
        if is_noise:
            db.save_triage_result(msg_id, account, sender, subject, date_str, level_0_status="filtered", triage_level=0, tag="low")
            if human_mode:
                EmailNotifier.print_level_0_hit(msg_id, account, subject, l0_reason)
            
            run_results.append({
                "triage_level": 0,
                "message_id": msg_id,
                "account": account,
                "sender": sender,
                "subject": subject,
                "date": date_str,
                "reason": l0_reason,
                "score": 0.0,
                "tag": "low"
            })
            stats["level_0_filtered"] += 1
            continue

        db.save_triage_result(msg_id, account, sender, subject, date_str, level_0_status="passed")

        # 3. Level 1 LLM Ternary Triage
        suggested_level, reason, score, l1_tag, l1_metrics = engine.run_level_1_classification(sender, subject, snippet)
        
        # Ambiguity Escalation Layer
        from config import settings
        if score < settings.triage.confidence_threshold:
            if human_mode:
                logger.info("Low confidence score (%s) from fast triage model. Escalating email to premium model...", score)
            full_id = email["id"]
            full_body = client_source.fetch_full_body(full_id)
            suggested_level, reason, score, l1_tag = engine.run_level_1_premium_escalation(sender, subject, snippet, full_body)
            reason = f"[Premium Escalated] {reason}"
        
        if suggested_level == 0:
            # Model downgraded this item to Level 0 noise
            db.save_triage_result(
                msg_id, account, sender, subject, date_str, 
                level_0_status="passed", level_1_status="downgraded",
                reason=reason, score=score, model_used_triage=settings.triage_model,
                level_1_duration_sec=l1_metrics["duration_sec"],
                level_1_prompt_tokens=l1_metrics["prompt_tokens"],
                level_1_completion_tokens=l1_metrics["completion_tokens"],
                triage_level=0, tag=l1_tag
            )
            if human_mode:
                logger.info("Level 1 model suggested downgrade to Level 0 noise for Message-ID: %s", msg_id)
                EmailNotifier.print_level_0_hit(msg_id, account, subject, reason)
            
            run_results.append({
                "triage_level": 0,
                "message_id": msg_id,
                "account": account,
                "sender": sender,
                "subject": subject,
                "date": date_str,
                "reason": reason,
                "score": score,
                "tag": l1_tag
            })
            stats["level_0_filtered"] += 1
            continue
        elif suggested_level == 1:
            db.save_triage_result(
                msg_id, account, sender, subject, date_str, 
                level_0_status="passed", level_1_status="unimportant",
                reason=reason, score=score, model_used_triage=settings.triage_model,
                level_1_duration_sec=l1_metrics["duration_sec"],
                level_1_prompt_tokens=l1_metrics["prompt_tokens"],
                level_1_completion_tokens=l1_metrics["completion_tokens"],
                triage_level=1, tag=l1_tag
            )
            if human_mode:
                EmailNotifier.print_level_1_hit(msg_id, account, subject, reason, score)
            
            run_results.append({
                "triage_level": 1,
                "message_id": msg_id,
                "account": account,
                "sender": sender,
                "subject": subject,
                "date": date_str,
                "reason": reason,
                "score": score,
                "tag": l1_tag
            })
            stats["level_1_unimportant"] += 1
            continue

        # 4. Level 2 Premium Summary (Only for Important Emails)
        stats["important_identified"] += 1
        
        # Fetch full body payload now that email has passed Level 1 triage
        full_id = email["id"]
        full_body = client_source.fetch_full_body(full_id)
        
        summary, summary_score, l2_tag, l2_metrics = engine.run_level_2_summarization(subject, full_body)
        
        db.save_triage_result(
            msg_id, account, sender, subject, date_str,
            level_0_status="passed", level_1_status="important", level_2_summary=summary,
            reason=reason, score=summary_score, model_used_triage=settings.triage_model,
            model_used_summary=settings.summary_model,
            level_1_duration_sec=l1_metrics["duration_sec"],
            level_1_prompt_tokens=l1_metrics["prompt_tokens"],
            level_1_completion_tokens=l1_metrics["completion_tokens"],
            level_2_duration_sec=l2_metrics["duration_sec"],
            level_2_prompt_tokens=l2_metrics["prompt_tokens"],
            level_2_completion_tokens=l2_metrics["completion_tokens"],
            triage_level=2, tag=l2_tag
        )
        
        # 5. Real-time Notification Alerts (Only printed if human mode requested)
        if human_mode:
            EmailNotifier.print_terminal_banner(subject, sender, reason, summary, summary_score)
        
        run_results.append({
            "triage_level": 2,
            "message_id": msg_id,
            "account": account,
            "sender": sender,
            "subject": subject,
            "date": date_str,
            "reason": reason,
            "summary": summary,
            "score": summary_score,
            "tag": l2_tag
        })

def filter_emails_by_days(emails: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    from datetime import datetime, timedelta, timezone
    import email.utils
    
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

def main() -> None:
    # Handle command-line arguments
    parser = argparse.ArgumentParser(description="Optimized Email Triage & Summarization Engine")
    parser.add_argument("--human", action="store_true", help="Output human readable logs and layout headers")
    parser.add_argument("--pretty", action="store_true", help="Pretty print the final JSON result array")
    parser.add_argument("--auth", action="store_true", help="Force full interactive Gmail OAuth re-authorization flow")
    parser.add_argument("--headless", action="store_true", help="Run OAuth authentication flow in headless/SSH console input mode")
    parser.add_argument("--max", type=int, help="Set maximum top n emails to read from EACH mail source")
    parser.add_argument("--days", type=int, help="Only output unread emails received within the last N days")
    parser.add_argument("--level", type=int, help="Only output JSON objects matching this specific triage level or higher")
    parser.add_argument("--compact", action="store_true", help="Emit a heavily minified JSON schema dropping non-essential fields to save tokens")
    parser.add_argument("--skip", type=int, default=0, help="Skip the first N processed emails for pagination")
    parser.add_argument("--limit", type=int, help="Limit the maximum number of output emails for pagination")
    parser.add_argument("--output", type=str, help="Write full verbose JSON array directly to disk and emit only a lightweight pointer summary to stdout")
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
        if args.days is not None:
            gmail_emails = filter_emails_by_days(gmail_emails, args.days)
        if args.max is not None:
            gmail_emails = gmail_emails[:args.max]
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
        if args.days is not None:
            imap_emails = filter_emails_by_days(imap_emails, args.days)
        if args.max is not None:
            imap_emails = imap_emails[:args.max]
        process_account_emails(imap_emails, imap, engine, db, stats, run_results, args.human)
    except Exception as e:
        if args.human:
            logger.error("Error during IMAP pipeline run: %s", e)

    # --- Output Run Content ---
    # Apply Option 1: Priority Level Filtering
    if args.level is not None:
        run_results = [r for r in run_results if (r.get("triage_level") if r.get("triage_level") is not None else 0) >= args.level]
        
    # Apply Option 3: Pagination Slicing
    if args.skip > 0:
        run_results = run_results[args.skip:]
    if args.limit is not None:
        run_results = run_results[:args.limit]
        
    # Apply Option 2: Compact Schema Minification
    if args.compact:
        compact_results = []
        for r in run_results:
            c_obj = {
                "mid": r.get("message_id"),
                "lvl": r.get("triage_level"),
                "snd": r.get("sender"),
                "sub": r.get("subject"),
                "dt": r.get("date"),
                "tag": r.get("tag")
            }
            if r.get("triage_level") == 2:
                c_obj["sum"] = r.get("summary")
            compact_results.append(c_obj)
        final_output_payload = compact_results
    else:
        final_output_payload = run_results

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
        # Apply Option 4: Output File Redirection + Metadata Pointer summary
        if args.output:
            from pathlib import Path
            out_p = Path(args.output).resolve()
            out_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_p, "w", encoding="utf-8") as f:
                if args.pretty:
                    json.dump(final_output_payload, f, indent=2, ensure_ascii=False)
                else:
                    json.dump(final_output_payload, f, ensure_ascii=False)
            
            pointer_summary = {
                "status": "success",
                "total_returned": len(final_output_payload),
                "file_uri": str(out_p)
            }
            if args.pretty:
                print(json.dumps(pointer_summary, indent=2, ensure_ascii=False))
            else:
                print(json.dumps(pointer_summary, ensure_ascii=False))
        else:
            # Standard execution JSON output mode ONLY: emit pure raw valid JSON without any extra text lines
            if args.pretty:
                print(json.dumps(final_output_payload, indent=2, ensure_ascii=False))
            else:
                print(json.dumps(final_output_payload, ensure_ascii=False))

if __name__ == "__main__":
    main()
