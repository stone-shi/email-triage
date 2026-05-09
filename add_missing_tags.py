import json
import logging
import argparse
import yaml
import sys
import time
from pathlib import Path
from typing import Dict, Any
from config import settings
from triage import EmailTriageEngine
from db import EmailDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] add_missing_tags: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("add_missing_tags")

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing tags for existing profile results")
    parser.add_argument("--profile", type=str, required=True, help="Name of the profile configuration to backfill")
    args = parser.parse_args()
    
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    emails_path = workspace_dir / "auto_rater_data" / "offline_emails.json"
    res_path = workspace_dir / "auto_rater_data" / f"auto_rater_results_{args.profile}.json"
    
    if not res_path.exists():
        logger.error("Result file not found: %s", res_path)
        sys.exit(1)
        
    if not config_path.exists() or not emails_path.exists():
        logger.error("Required config or offline emails file missing.")
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    configs = config_data.get("test_configurations", [])
    target_cfg = next((c for c in configs if c.get("name") == args.profile), None)
    
    with open(res_path, "r", encoding="utf-8") as f:
        res_payload = json.load(f)
        
    triage_model = res_payload.get("triage_model")
    summary_model = res_payload.get("summary_model")
    
    if target_cfg:
        triage_model = target_cfg.get("triage_model", triage_model)
        summary_model = target_cfg.get("summary_model", summary_model)
        
    logger.info("Backfilling tags for profile '%s' using Triage: %s | Summary: %s", args.profile, triage_model, summary_model)
    
    with open(emails_path, "r", encoding="utf-8") as f:
        emails_list = json.load(f)
    emails_by_id = {e["message_id"]: e for e in emails_list}
    
    db = EmailDB(db_path=workspace_dir / "email_cache.db")
    engine = EmailTriageEngine(db)
    
    updated_count = 0
    results = res_payload.get("results", [])
    
    for idx, item in enumerate(results, 1):
        current_tag = item.get("tag")
        if current_tag and current_tag != "un-tagged":
            continue
            
        msg_id = item["message_id"]
        t_level = item.get("triage_level", 0)
        sender = item.get("sender", "unknown")
        subject = item.get("subject", "")
        date_str = item.get("date", "")
        
        orig_email = emails_by_id.get(msg_id, {})
        snippet = orig_email.get("snippet", subject)
        full_body = orig_email.get("full_body", "")
        
        logger.info("Evaluating missing tag for item %d/%d (Message-ID: %s, Level: %d)", idx, len(results), msg_id, t_level)
        
        if t_level == 0:
            item["tag"] = "low"
            db.save_triage_result(msg_id, item.get("account", ""), sender, subject, date_str, level_0_status="filtered", triage_level=0, tag="low")
            updated_count += 1
        elif t_level == 1:
            try:
                suggested_level, reason, score, tag_str, _ = engine.run_level_1_classification(sender, subject, snippet, model_name=triage_model)
                item["tag"] = tag_str
                item["reason"] = reason
                item["score"] = score
                if suggested_level == 0:
                    logger.info("Downgrading item %s from Level 1 to Level 0 noise during backfill!", msg_id)
                    item["triage_level"] = 0
                elif suggested_level == 2:
                    logger.info("Escalating item %s from Level 1 to Level 2 during tag backfill evaluation!", msg_id)
                    item["triage_level"] = 2
                    if full_body and len(full_body.strip()) >= 10:
                        summary, sum_score, l2_tag, _ = engine.run_level_2_summarization(subject, full_body, model_name=summary_model)
                        item["summary"] = summary
                        item["score"] = sum_score
                        item["tag"] = l2_tag
                
                lvl_status = "downgraded" if item["triage_level"] == 0 else ("important" if item["triage_level"] == 2 else "unimportant")
                db.save_triage_result(msg_id, item.get("account", ""), sender, subject, date_str, level_0_status="passed", level_1_status=lvl_status, level_2_summary=item.get("summary"), reason=item["reason"], score=item["score"], triage_level=item["triage_level"], tag=item["tag"])
                updated_count += 1
                time.sleep(0.5)
            except Exception as e:
                logger.error("Failed to evaluate Level 1 tag for %s: %s", msg_id, e)
        elif t_level == 2:
            try:
                if engine.is_vip_sender(sender):
                    item["tag"] = "vip"
                else:
                    if full_body and len(full_body.strip()) >= 10:
                        summary, sum_score, tag_str, _ = engine.run_level_2_summarization(subject, full_body, model_name=summary_model)
                        item["summary"] = summary
                        item["score"] = sum_score
                        item["tag"] = tag_str
                    else:
                        item["tag"] = "notification"
                db.save_triage_result(msg_id, item.get("account", ""), sender, subject, date_str, level_0_status="passed", level_1_status="important", level_2_summary=item.get("summary"), reason=item.get("reason"), score=item.get("score"), triage_level=2, tag=item["tag"])
                updated_count += 1
                time.sleep(0.5)
            except Exception as e:
                logger.error("Failed to evaluate Level 2 tag for %s: %s", msg_id, e)
                
    if updated_count > 0:
        with open(res_path, "w", encoding="utf-8") as f:
            json.dump(res_payload, f, indent=2, ensure_ascii=False)
        logger.info("Successfully backfilled %d missing tags for profile '%s'.", updated_count, args.profile)
    else:
        logger.info("All items in profile '%s' already have valid tags. No changes needed.", args.profile)

if __name__ == "__main__":
    main()
