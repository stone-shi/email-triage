import json
import logging
import argparse
import yaml
import sys
from pathlib import Path
from typing import List, Dict, Any
from gmail_client import GmailClient
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_downloader: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_downloader")

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    output_dir = workspace_dir / "auto_rater_data"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "offline_emails.json"
    
    if not config_path.exists():
        logger.error("Configuration file not found at %s", config_path)
        sys.exit(1)
        
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Auto Rater Offline Dataset Downloader Utility")
    parser.add_argument("--sender", type=str, help="Filter by sender email address")
    parser.add_argument("--receiver", type=str, help="Filter by receiver email address")
    parser.add_argument("--subject", type=str, help="Filter by target subject line (automatically normalizes Re: Re: prefixes)")
    args = parser.parse_args()
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
        
    log_level = config.get("log_level", "INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    logger.setLevel(numeric_level)
        
    count = config.get("download_count", 20)
    
    # Build Gmail API search query string dynamically
    import re
    query_parts = []
    if args.sender:
        query_parts.append(f"from:{args.sender}")
    if args.receiver:
        query_parts.append(f"to:{args.receiver}")
    if args.subject:
        # Repeatedly strip out leading case-insensitive 're:' or 'fwd:' prefixes and spaces
        base_subject = re.sub(r'^(?i:\s*(?:re|fwd)\s*:\s*)+', '', args.subject).strip()
        query_parts.append(f'subject:\"{base_subject}\"')
        
    query_str = " ".join(query_parts) if query_parts else None
    
    if query_str:
        logger.info("Targeted thread search active with query: '%s'", query_str)
    else:
        logger.info("Starting generic offline download of top %d emails from Gmail...", count)
    
    # Initialize Gmail client (handles authentication internally)
    try:
        gmail = GmailClient()
    except Exception as e:
        logger.error("Failed to initialize GmailClient: %s", e)
        sys.exit(1)
        
    if not gmail.service:
        logger.error("Gmail service client not available.")
        sys.exit(1)
        
    existing_ids = set()
    offline_dataset: List[Dict[str, Any]] = []
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                offline_dataset = json.load(f)
            existing_ids = {e["id"] for e in offline_dataset}
            logger.info("Loaded %d existing unique cached items from offline corpus.", len(offline_dataset))
        except Exception:
            offline_dataset = []
            
    try:
        # List the latest messages matching the query up to the specified count
        logger.info("Listing messages from user profile...")
        response = gmail.service.users().messages().list(userId="me", maxResults=count, q=query_str).execute()
        messages = response.get("messages", [])
        
        if not messages:
            logger.warning("No messages found in the account.")
            sys.exit(0)
            
        logger.info("Found %d messages. Downloading metadata and full bodies...", len(messages))
        
        for idx, msg in enumerate(messages, 1):
            msg_id = msg["id"]
            if msg_id in existing_ids:
                logger.info("[%d/%d] Skipping already cached email ID: %s", idx, len(messages), msg_id)
                continue
                
            try:
                # Fetch metadata headers
                msg_meta = gmail.service.users().messages().get(
                    userId="me", id=msg_id, format="metadata",
                    metadataHeaders=["Message-ID", "From", "Subject", "Date"]
                ).execute()
                
                headers = msg_meta.get("payload", {}).get("headers", [])
                header_dict = {h["name"]: h["value"] for h in headers}
                
                message_id = header_dict.get("Message-ID", msg_id)
                from_str = header_dict.get("From", "Unknown Sender")
                subject_str = header_dict.get("Subject", "(No Subject)")
                date_str = header_dict.get("Date", "")
                snippet_str = msg_meta.get("snippet", "")
                
                logger.info("[%d/%d] Fetching body for message: %s", idx, len(messages), subject_str)
                
                # Fetch full text body
                full_body = gmail.fetch_full_body(msg_id)
                
                offline_dataset.append({
                    "id": msg_id,
                    "message_id": message_id,
                    "sender": from_str,
                    "subject": subject_str,
                    "date": date_str,
                    "snippet": snippet_str,
                    "account": settings.gmail_account,
                    "full_body": full_body
                })
            except Exception as inner_e:
                logger.error("Failed to download message %s: %s", msg_id, inner_e)
                continue
                
        with open(output_path, "w", encoding="utf-8") as out_f:
            json.dump(offline_dataset, out_f, indent=2, ensure_ascii=False)
            
        logger.info("Successfully saved %d emails offline to %s", len(offline_dataset), output_path)
        
    except Exception as e:
        logger.error("An error occurred during batch ingestion: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
