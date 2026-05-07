import json
import logging
import argparse
import yaml
import sys
import time
from pathlib import Path
from typing import List, Dict, Any
import httpx
from config import settings
from triage import EmailTriageEngine
from db import EmailDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_runner: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_runner")

def extract_json(text: str) -> str:
    import re
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text

def run_config(config: Dict[str, Any], emails: List[Dict[str, Any]], workspace_dir: Path, judge_model: str, level_0_judge_model: str, force_rerun: bool = False, max_items: int = None) -> None:
    config_name = config["name"]
    triage_model = config["triage_model"]
    summary_model = config["summary_model"]
    
    output_file = workspace_dir / "auto_rater_data" / f"auto_rater_results_{config_name}.json"
    
    existing_results: Dict[str, Dict[str, Any]] = {}
    existing_total_duration = 0.0
    if output_file.exists() and not force_rerun:
        try:
            with open(output_file, "r", encoding="utf-8") as out_f:
                old_payload = json.load(out_f)
            existing_results = {r["message_id"]: r for r in old_payload.get("results", [])}
            existing_total_duration = old_payload.get("total_processing_all_emails_duration_sec", 0.0)
            logger.info("Incremental Mode Active: Loaded %d already processed items from cache.", len(existing_results))
        except Exception:
            existing_results = {}
            existing_total_duration = 0.0

    logger.info("==================================================")
    logger.info("Executing Test Configuration: '%s'", config_name)
    logger.info("Triage Model: %s | Summary Model: %s", triage_model, summary_model)
    logger.info("==================================================")
    
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}"
    }
    
    # Load external prompts if present
    prompts_path = workspace_dir / "prompts.yml"
    prompts = {}
    try:
        if prompts_path.exists():
            with open(prompts_path, "r", encoding="utf-8") as f:
                prompts = yaml.safe_load(f) or {}
    except Exception:
        pass
    
    http_client = httpx.Client(timeout=1800.0)
    run_results: List[Dict[str, Any]] = []
    
    # Initialize triage engine for static filtering logic (Level 0)
    dummy_db = EmailDB(db_path=workspace_dir / "email_cache.db")
    engine = EmailTriageEngine(dummy_db)
    
    new_emails_duration = 0.0
    processed_any_new = False
    
    l0_processed = 0
    l1_processed = 0
    l2_processed = 0
    
    for idx, email in enumerate(emails, 1):
        if max_items is not None and l0_processed >= max_items and l1_processed >= max_items and l2_processed >= max_items:
            logger.info("Max items reached for all levels. Stopping processing.")
            break
        sender = email["sender"]
        subject = email["subject"]
        snippet = email["snippet"]
        full_body = email["full_body"]
        msg_id = email["message_id"]
        
        # Cache Skip Layer Conditional Check: Unchanged Model + Message ID cached match
        if msg_id in existing_results and not force_rerun:
            run_results.append(existing_results[msg_id])
            continue
            
        email_start_time = time.time()
        
        # Initialize default metrics record matching user requirements
        metrics = {
            "triage_level": 0,
            "message_id": msg_id,
            "account": email["account"],
            "sender": sender,
            "subject": subject,
            "date": email["date"],
            "reason": "Passed static filter",
            "summary": None,
            "score": 1.0,
            "model_used_triage": triage_model,
            "model_used_summary": summary_model,
            "level_1_duration_sec": 0.0,
            "level_2_duration_sec": 0.0,
            "level_1_prompt_tokens": 0,
            "level_1_completion_tokens": 0,
            "level_2_prompt_tokens": 0,
            "level_2_completion_tokens": 0,
            "total_email_process_duration_sec": 0.0,
            "level_0_judge_correctness": "N/A",
            "level_0_judge_score": 1.0,
            "level_0_judge_reason": None
        }
        
        # VIP Whitelist Override Layer -> Direct to Level 2
        if engine.is_vip_sender(sender):
            if max_items is not None and l2_processed >= max_items:
                continue
            l2_processed += 1
            logger.info("VIP hit: Sender '%s' is a whitelisted VIP. Bypassing Level 0 and Level 1 directly to Level 2!", sender)
            metrics["triage_level"] = 2
            metrics["reason"] = "VIP Sender Direct Escalation"
            
            if not full_body or len(full_body.strip()) < 10:
                metrics["summary"] = "No substantive content to summarize."
            else:
                l2_prompt = f"Subject: {subject}\nBody:\n{full_body[:8000]}"
                l2_system = prompts.get("level_2_summarization", {}).get("system", "")
                l2_start = time.time()
                try:
                    l2_payload = {
                        "model": summary_model,
                        "messages": [
                            {"role": "system", "content": l2_system},
                            {"role": "user", "content": l2_prompt}
                        ],
                        "temperature": 0.2
                    }
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                    resp.raise_for_status()
                    resp_json = resp.json()
                    
                    usage = resp_json.get("usage", {})
                    metrics["level_2_prompt_tokens"] = usage.get("prompt_tokens", 0)
                    metrics["level_2_completion_tokens"] = usage.get("completion_tokens", 0)
                    
                    content = resp_json["choices"][0]["message"]["content"]
                    result_dict = json.loads(extract_json(content))
                    
                    metrics["summary"] = result_dict.get("summary", "")
                    metrics["score"] = result_dict.get("confidence_score", 1.0)
                except Exception as e:
                    logger.error("Level 2 VIP summary failed for email %s: %s", msg_id, e)
                    metrics["summary"] = f"Level 2 summarization error: {str(e)}"
                    
                metrics["level_2_duration_sec"] = time.time() - l2_start
                
            metrics["total_email_process_duration_sec"] = time.time() - email_start_time
            run_results.append(metrics)
            continue

        # 1. Level 0 Static Filter
        is_noise, l0_reason = engine.run_level_0_static(sender, subject)
        if is_noise:
            if max_items is not None and l0_processed >= max_items:
                continue
            l0_processed += 1
            metrics["triage_level"] = 0
            metrics["reason"] = l0_reason
            
            # Use judge_model to verify if the Level 0 filter was actually correct
            l0_audit_prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
            l0_audit_system = prompts.get("auto_rater_level_0_audit", {}).get("system")
            if not l0_audit_system:
                l0_audit_system = (
                    "You are an expert email auditor. Review the email metadata to determine if it is truly low priority noise "
                    "(e.g., automated notifications, transactional marketing, newsletters, spam) or if it was a false positive "
                    "that actually contains high priority business communication or a critical personal update.\n"
                    "You MUST return a valid JSON object containing exactly three fields: "
                    "'is_actually_low_priority' (boolean), 'reason' (string), and 'confidence_score' (float from 0.0 to 1.0)."
                )
            try:
                l0_payload = {
                    "model": level_0_judge_model,
                    "messages": [
                        {"role": "system", "content": l0_audit_system},
                        {"role": "user", "content": l0_audit_prompt}
                    ],
                    "temperature": 0.0
                }
                resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l0_payload)
                resp.raise_for_status()
                audit_dict = json.loads(extract_json(resp.json()["choices"][0]["message"]["content"]))
                
                metrics["level_0_judge_correctness"] = "Correct" if audit_dict.get("is_actually_low_priority", True) else "False Positive"
                metrics["level_0_judge_score"] = audit_dict.get("confidence_score", 1.0)
                metrics["level_0_judge_reason"] = audit_dict.get("reason", "")
            except Exception as audit_err:
                logger.error("Level 0 judge audit failed: %s", audit_err)
                metrics["level_0_judge_correctness"] = "Audit Failed"
                metrics["level_0_judge_score"] = 0.0
                metrics["level_0_judge_reason"] = str(audit_err)
                continue # Skip caching if judge audit failed

            metrics["total_email_process_duration_sec"] = time.time() - email_start_time
            new_emails_duration += metrics["total_email_process_duration_sec"]
            processed_any_new = True
            run_results.append(metrics)
            continue
            
        # 2. Level 1 LLM / TEI Ingestion Classification
        if max_items is not None and l1_processed >= max_items:
            continue
        l1_processed += 1
        l1_is_important, reason, score, l1_metrics = engine.run_level_1_classification(sender, subject, snippet, model_name=triage_model)
        
        metrics["reason"] = reason
        metrics["score"] = score
        metrics["triage_level"] = 1
        metrics["level_1_duration_sec"] = l1_metrics["duration_sec"]
        metrics["level_1_prompt_tokens"] = l1_metrics["prompt_tokens"]
        metrics["level_1_completion_tokens"] = l1_metrics["completion_tokens"]
        
        # Check for endpoint failures to support resume capability
        if "Proxy error:" in reason or "TEI server prediction error:" in reason:
            logger.warning("Omitting email %s from results cache due to runtime LLM endpoint error.", msg_id)
            continue
            
        # 3. Level 2 Premium Summary (only if Level 1 marked important)
        if l1_is_important:
            if max_items is not None and l2_processed >= max_items:
                # Skip L2 summary to save time, keep as Level 1
                pass
            else:
                l2_processed += 1
                metrics["triage_level"] = 2
                if not full_body or len(full_body.strip()) < 10:
                    metrics["summary"] = "No substantive content to summarize."
                else:
                    summary, summary_score, l2_metrics = engine.run_level_2_summarization(subject, full_body, model_name=summary_model)
                    
                    # Check for endpoint failures
                    if "Failed to generate proxy summary due to error" in summary:
                        logger.warning("Omitting email %s from results cache due to Level 2 summarization failure.", msg_id)
                        continue
                        
                    metrics["summary"] = summary
                    metrics["score"] = summary_score
                    metrics["level_2_duration_sec"] = l2_metrics["duration_sec"]
                    metrics["level_2_prompt_tokens"] = l2_metrics["prompt_tokens"]
                    metrics["level_2_completion_tokens"] = l2_metrics["completion_tokens"]
                
        metrics["total_email_process_duration_sec"] = time.time() - email_start_time
        new_emails_duration += metrics["total_email_process_duration_sec"]
        processed_any_new = True
        run_results.append(metrics)
        
    if processed_any_new:
        total_duration = existing_total_duration + new_emails_duration
    else:
        total_duration = existing_total_duration
    
    # Package wrapper container with complete benchmark group telemetry metadata
    output_payload = {
        "configuration_name": config_name,
        "triage_model": triage_model,
        "summary_model": summary_model,
        "total_processing_all_emails_duration_sec": total_duration,
        "total_emails_processed": len(emails),
        "results": run_results
    }
    
    output_file.parent.mkdir(exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as out_f:
        json.dump(output_payload, out_f, indent=2, ensure_ascii=False)
        
    logger.info("Finished test run for '%s'. Results saved pretty to %s", config_name, output_file)

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    emails_path = workspace_dir / "auto_rater_data" / "offline_emails.json"
    
    if not config_path.exists() or not emails_path.exists():
        logger.error("Required files missing. Make sure config and offline_emails.json exist.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    # Dynamically calibrate logging thresholds
    log_level = config_data.get("log_level", "INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    logger.setLevel(numeric_level)
        
    with open(emails_path, "r") as f:
        emails = json.load(f)
        
    configs = config_data.get("test_configurations", [])
    judge_model = config_data.get("judge_model", "deepseek/deepseek-v4-pro")
    level_0_judge_model = config_data.get("level_0_judge_model", judge_model)
    if not configs:
        logger.error("No test configurations found in config file.")
        sys.exit(1)
        
    parser = argparse.ArgumentParser(description="Auto Rater Benchmarking Runner")
    parser.add_argument("--run", type=str, help="Name of a single test configuration pair to execute specifically")
    parser.add_argument("-f", "--force", action="store_true", help="Force execution and overwrite existing benchmark results file")
    parser.add_argument("--max-items", type=int, help="Maximum items to process per triage level tier (useful for fast testing)")
    args = parser.parse_args()
    
    if args.run:
        configs = [c for c in configs if c.get("name") == args.run]
        if not configs:
            logger.error("No test configuration found matching name: '%s'", args.run)
            sys.exit(1)
        logger.info("Targeted single configuration run: '%s'", args.run)
        
    logger.info("Loaded %d offline emails. Starting benchmarking configurations...", len(emails))
    
    for cfg in configs:
        cfg_name = cfg.get("name")
        triage_model = cfg.get("triage_model")
        summary_model = cfg.get("summary_model")
        output_file = workspace_dir / "auto_rater_data" / f"auto_rater_results_{cfg_name}.json"
        
        if output_file.exists():
            try:
                with open(output_file, "r", encoding="utf-8") as out_f:
                    existing_data = json.load(out_f)
            except Exception:
                existing_data = {}
            # 2. Model Definition Modifications Guard Abort Check
            if existing_data.get("triage_model") != triage_model or existing_data.get("summary_model") != summary_model:
                if not args.force:
                    logger.error("⚠️ WARNING: Model configuration strings changed for profile '%s' (Triage: %s -> %s, Summary: %s -> %s). Execution aborted to protect data integrity. Use -f/--force to override and overwrite.", cfg_name, existing_data.get("triage_model"), triage_model, existing_data.get("summary_model"), summary_model)
                    sys.exit(1)
                logger.info("Force override active: Overwriting modified model pairs for configuration '%s'...", cfg_name)
        
        try:
            run_config(cfg, emails, workspace_dir, judge_model, level_0_judge_model, force_rerun=args.force, max_items=args.max_items)
        except Exception as e:
            logger.error("Configuration run failed for %s: %s", cfg_name, e)
            continue

if __name__ == "__main__":
    main()
