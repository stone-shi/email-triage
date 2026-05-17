import json
import logging
import argparse
import yaml
import sys
import time
import httpx
from pathlib import Path
from typing import Dict, Any, List

# Reuse prompt extraction helper from runner if possible, or replicate
def extract_json(text: str) -> str:
    import re
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    
    # Robustness fix: handle unquoted tags from lazy models
    text = re.sub(r'("tag":\s*)(?!(?:true|false|null)\b)([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*[,}])', r'\1"\2"', text)
    return text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_upgrader: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_upgrader")

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    data_dir = workspace_dir / "auto_rater_data"
    emails_path = data_dir / "offline_emails.json"
    prompts_path = workspace_dir / "prompts.yml"
    
    # Handle arguments
    parser = argparse.ArgumentParser(description="Auto Rater Results Maintenance Upgrader")
    parser.add_argument("--profile", type=str, help="Specific target auto_rater_results_*.json file name base (e.g. production_deepseek_pair) to upgrade")
    args = parser.parse_args()
    
    if not config_path.exists() or not emails_path.exists():
        logger.error("Required configuration or offline emails files are missing.")
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    with open(emails_path, "r", encoding="utf-8") as f:
        emails_list = json.load(f)
    emails_by_id = {e["message_id"]: e for e in emails_list}
    
    prompts = {}
    if prompts_path.exists():
        with open(prompts_path, "r", encoding="utf-8") as f:
            prompts = yaml.safe_load(f) or {}
            
    level_0_judge_model = config_data.get("level_0_judge_model", "deepseek/deepseek-v4-flash")
    judge_model = config_data.get("judge_model", "gemini/gemini-3.1-pro-preview")
    
    # Locate results files
    if args.profile:
        result_files = [data_dir / f"auto_rater_results_{args.profile}.json"]
    else:
        result_files = list(data_dir.glob("auto_rater_results_*.json"))
        
    if not result_files:
        logger.error("No result files found to upgrade.")
        sys.exit(1)
        
    from config import settings
    from triage import EmailTriageEngine
    from db import EmailDB
    
    # Instantiate temporary DB connection for engine (logs usage tokens)
    db = EmailDB()
    engine = EmailTriageEngine(db)
    
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}"
    }
    http_client = httpx.Client(timeout=1800.0)
    
    for res_file in result_files:
        if not res_file.exists():
            logger.warning("File %s does not exist, skipping.", res_file.name)
            continue
            
        logger.info("Processing results file: %s", res_file.name)
        with open(res_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
            
        results = payload.get("results", [])
        triage_model = payload.get("triage_model", settings.triage_model)
        summary_model = payload.get("summary_model", settings.summary_model)
        
        modified_count = 0
        
        for idx, r in enumerate(results):
            msg_id = r["message_id"]
            current_level = r["triage_level"]
            
            if current_level not in [0, 1, "Level 0", "Level 1"]:
                continue
                
            original_email = emails_by_id.get(msg_id)
            if not original_email:
                continue
                
            sender = original_email["sender"]
            subject = original_email["subject"]
            snippet = original_email["snippet"]
            full_body = original_email["full_body"]
            
            # VIP Whitelist Override Layer -> Direct to Level 2
            if engine.is_vip_sender(sender):
                if current_level != 2 and current_level != "Level 2":
                    logger.info("Email '%s' from VIP sender. Escalating to Level 2 directly...", subject)
                    r["triage_level"] = 2
                    r["reason"] = "VIP Sender Direct Escalation"
                    
                    l2_prompt = f"Subject: {subject}\nBody:\n{full_body[:8000]}"
                    l2_system = prompts.get("level_2_summarization", {}).get("system", "")
                    
                    try:
                        l2_payload = {
                            "model": summary_model,
                            "messages": [
                                {"role": "system", "content": l2_system},
                                {"role": "user", "content": l2_prompt}
                            ],
                            "temperature": 0.2,
                            "include_reasoning": False
                        }
                        resp_l2 = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                        resp_l2.raise_for_status()
                        content_l2 = resp_l2.json()["choices"][0]["message"]["content"]
                        result_dict_l2 = json.loads(extract_json(content_l2))
                        
                        r["summary"] = result_dict_l2.get("summary", "")
                        r["score"] = result_dict_l2.get("confidence_score", 1.0)
                        
                        r.pop("level_0_judge_correctness", None)
                        r.pop("level_0_judge_score", None)
                        r.pop("level_0_judge_reason", None)
                        
                        modified_count += 1
                        
                        payload["results"] = results
                        with open(res_file, "w", encoding="utf-8") as out_f:
                            json.dump(payload, out_f, indent=2, ensure_ascii=False)
                            
                    except Exception as e:
                        logger.error("Failed to generate Level 2 summary for VIP email %s: %s", msg_id, e)
                        
                continue
            
            # Rerun Level 0 Static Filter
            is_noise, l0_reason = engine.run_level_0_static(sender, subject)
            
            new_level = "Level 1"
            if is_noise:
                new_level = "Level 0"
                
            # Case A: Shifted from Level 1 to Level 0 (New regex caught it)
            if (current_level == 1 or current_level == "Level 1") and new_level == "Level 0":
                logger.info("Email '%s' shifted L1 -> L0. Regenerating judge audit...", subject)
                r["triage_level"] = 0
                r["reason"] = l0_reason
                
                # Call smaller judge to audit Level 0
                l0_audit_prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
                l0_audit_system = prompts.get("auto_rater_level_0_audit", {}).get("system", "")
                try:
                    l0_payload = {
                        "model": level_0_judge_model,
                        "messages": [
                            {"role": "system", "content": l0_audit_system},
                            {"role": "user", "content": l0_audit_prompt}
                        ],
                        "temperature": 0.0,
                        "include_reasoning": False
                    }
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l0_payload)
                    resp.raise_for_status()
                    audit_dict = json.loads(extract_json(resp.json()["choices"][0]["message"]["content"]))
                    
                    r["level_0_judge_correctness"] = "Correct" if audit_dict.get("is_actually_low_priority", True) else "False Positive"
                    r["level_0_judge_score"] = audit_dict.get("confidence_score", 1.0)
                    r["level_0_judge_reason"] = audit_dict.get("reason", "")
                except Exception as e:
                    logger.error("L0 judge audit failed for shifted email %s: %s", msg_id, e)
                    r["level_0_judge_correctness"] = "Audit Failed"
                    
                modified_count += 1
                
            # Case B: Shifted from Level 0 to Level 1 or 2 (Removed regex allowed it)
            elif (current_level == 0 or current_level == "Level 0") and new_level == "Level 1":
                logger.info("Email '%s' shifted L0 -> L1. Running model triage...", subject)
                
                # Run Level 1 LLM Classification
                l1_prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
                l1_system = prompts.get("level_1_fast_triage", {}).get("system", "")
                
                try:
                    l1_payload = {
                        "model": triage_model,
                        "messages": [
                            {"role": "system", "content": l1_system},
                            {"role": "user", "content": l1_prompt}
                        ],
                        "temperature": 0.0,
                        "include_reasoning": False
                    }
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l1_payload)
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    result_dict = json.loads(extract_json(content))
                    
                    l1_is_important = result_dict.get("is_important", True)
                    r["reason"] = result_dict.get("reason", "No reason provided")
                    r["score"] = result_dict.get("confidence_score", 1.0)
                    r["triage_level"] = 1
                    
                    # Clean Level 0 judge leftovers if present
                    r.pop("level_0_judge_correctness", None)
                    r.pop("level_0_judge_score", None)
                    r.pop("level_0_judge_reason", None)
                    
                    # If important, run Level 2 Summarization
                    if l1_is_important and len(full_body.strip()) > 10:
                        logger.info("Email '%s' marked important. Generating summary...", subject)
                        r["triage_level"] = 2
                        
                        l2_prompt = f"Subject: {subject}\nBody:\n{full_body[:8000]}"
                        l2_system = prompts.get("level_2_summarization", {}).get("system", "")
                        
                        l2_payload = {
                            "model": summary_model,
                            "messages": [
                                {"role": "system", "content": l2_system},
                                {"role": "user", "content": l2_prompt}
                            ],
                            "temperature": 0.2,
                            "include_reasoning": False
                        }
                        resp_l2 = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                        resp_l2.raise_for_status()
                        content_l2 = resp_l2.json()["choices"][0]["message"]["content"]
                        result_dict_l2 = json.loads(extract_json(content_l2))
                        
                        r["summary"] = result_dict_l2.get("summary", "")
                        r["score"] = result_dict_l2.get("confidence_score", 1.0)
                        
                except Exception as e:
                    logger.error("Failed to process shifted email %s: %s", msg_id, e)
                    # Keep as Level 0 or set error? Let's keep it as Level 0 so it can be retried
                    continue
                    
                modified_count += 1
                
            # Immediate Write to disk on successful resolution of this email
            if modified_count > 0:
                payload["results"] = results
                with open(res_file, "w", encoding="utf-8") as out_f:
                    json.dump(payload, out_f, indent=2, ensure_ascii=False)
                    
        logger.info("Finished upgrading file %s. Modified %d records.", res_file.name, modified_count)

if __name__ == "__main__":
    main()
