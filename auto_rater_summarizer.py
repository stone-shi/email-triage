import json
import logging
import argparse
import yaml
import sys
import time
from pathlib import Path
from typing import Dict, Any, List
import httpx
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_summarizer: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_summarizer")

def extract_json(text: str) -> str:
    import re
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    data_dir = workspace_dir / "auto_rater_data"
    emails_path = data_dir / "offline_emails.json"
    result_files = list(data_dir.glob("auto_rater_results_*.json"))
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Auto Rater Summarization Report Compiler Utility")
    parser.add_argument("--compare", type=str, help="Name of a single experimental result configuration to evaluate specifically")
    args = parser.parse_args()
    
    if args.compare:
        result_files = [f for f in result_files if f.name == f"auto_rater_results_{args.compare}.json"]
        if not result_files:
            logger.error("No result data file found for configuration: '%s'", args.compare)
            sys.exit(1)
        logger.info("Targeted quality evaluation active for configuration: '%s'", args.compare)
        
    if not config_path.exists() or not emails_path.exists() or not result_files:
        logger.error("Required testing files or configuration data are missing.")
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    log_level = config_data.get("log_level", "INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    logger.setLevel(numeric_level)
        
    with open(emails_path, "r") as f:
        emails_list = json.load(f)
        
    emails_by_id = {e["message_id"]: e for e in emails_list}
    judge_model = config_data.get("judge_model", "deepseek/deepseek-v4-pro")
    
    logger.info("Initializing Summary Quality Rater using Judge Model: %s", judge_model)
    
    # Load or Initialize local cache database registry
    cache_path = data_dir / "auto_rater_summarizer_cache.json"
    try:
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as cache_f:
                judge_cache = json.load(cache_f)
            logger.info("Loaded %d existing quality score cache records from disk.", len(judge_cache))
        else:
            judge_cache = {}
    except Exception:
        judge_cache = {}
    
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}"
    }
    http_client = httpx.Client(timeout=1800.0)
    
    markdown_lines = []
    markdown_lines.append("# 📝 Auto Rater: High-Fidelity Executive Summarization Quality Report")
    markdown_lines.append(f"Evaluated with LLM-as-a-Judge using model: `{judge_model}`.\n")
    
    for res_file in result_files:
        with open(res_file, "r") as f:
            res_payload = json.load(f)
            
        config_name = res_payload["configuration_name"]
        result_model = res_payload.get("triage_model", "unknown")
        results = res_payload["results"]
        
        markdown_lines.append(f"## 📌 Configuration Group: `{config_name}`")
        markdown_lines.append("| Email Subject | Accuracy (1-10) | Conciseness (1-10) | Actionability (1-10) | Judge Rationale |")
        markdown_lines.append("|---|---|---|---|---|")
        
        total_acc, total_con, total_act, scored_count = 0, 0, 0, 0
        
        for r in results:
            if r["triage_level"] != "Level 2" or not r.get("summary"):
                continue
                
            msg_id = r["message_id"]
            original_email = emails_by_id.get(msg_id)
            if not original_email:
                continue
                
            subject = r["subject"]
            summary_text = r["summary"]
            full_body_text = original_email["full_body"]
            
            # Composite Primary Key lookup match query signature string
            cache_key = f"{result_model}||{judge_model}||{msg_id}||{summary_text}"
            
            cache_hit = False
            if cache_key in judge_cache:
                logger.info("Cache Hit: Reusing cached quality metrics for email: '%s'", subject)
                scores = judge_cache[cache_key].get("scores", {})
                cache_hit = True
            
            try:
                if not cache_hit:
                    judge_prompt = (
                        f"Original Email Subject: {subject}\n"
                        f"Original Email Body:\n{full_body_text[:4000]}\n\n"
                        f"Generated Summary under Test:\n{summary_text}\n"
                    )
                    judge_system = (
                        "You are a strict supervisor auditing executive assistant performance. Score the generated email summary "
                        "on a 1-10 integer scale across three categories: "
                        "1. 'accuracy' (factually true to the body), "
                        "2. 'conciseness' (short, crisp, bulleted without fluff), "
                        "3. 'actionability' (clearly surfaces tasks, key decisions, and deadlines).\n"
                        "You MUST return a valid JSON object containing exactly four fields: "
                        "'accuracy' (int), 'conciseness' (int), 'actionability' (int), and 'rationale' (string explaining the scores)."
                    )
                    
                    payload = {
                        "model": judge_model,
                        "messages": [
                            {"role": "system", "content": judge_system},
                            {"role": "user", "content": judge_prompt}
                        ],
                        "temperature": 0.0
                    }
                    logger.info("Requesting quality score from judge for email: %s", subject)
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                    resp.raise_for_status()
                    
                    judge_resp = resp.json()
                    usage = judge_resp.get("usage", {})
                    content = judge_resp["choices"][0]["message"]["content"]
                    scores = json.loads(extract_json(content))
                    
                    # Immediate Cache Ingestion & Flush serialization to disk
                    judge_cache[cache_key] = {
                        "scores": scores,
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0)
                    }
                    with open(cache_path, "w", encoding="utf-8") as cache_f:
                        json.dump(judge_cache, cache_f, indent=2, ensure_ascii=False)
                
                acc = scores.get("accuracy", 10)
                con = scores.get("conciseness", 10)
                act = scores.get("actionability", 10)
                rat = scores.get("rationale", "N/A")
                
                total_acc += acc
                total_con += con
                total_act += act
                scored_count += 1
                
                markdown_lines.append(f"| {subject} | {acc}/10 | {con}/10 | {act}/10 | {rat} |")
            except Exception as e:
                logger.error("Failed to judge summary quality for email '%s': %s", subject, e)
                if 'content' in locals():
                    logger.error("Raw unparsed judge response was: \n%s", content)
                markdown_lines.append(f"| {subject} | Error | Error | Error | Audit call failed: {str(e)} |")
                
        if scored_count > 0:
            avg_acc = total_acc / scored_count
            avg_con = total_con / scored_count
            avg_act = total_act / scored_count
            markdown_lines.append(f"\n### 📊 Aggregate Score Averages for `{config_name}`:")
            markdown_lines.append(f"- **Average Summary Accuracy**: {avg_acc:.2f}/10")
            markdown_lines.append(f"- **Average Summary Conciseness**: {avg_con:.2f}/10")
            markdown_lines.append(f"- **Average Summary Actionability**: {avg_act:.2f}/10\n")
        else:
            markdown_lines.append("\n*No escalated summaries generated to evaluate for this configuration.*\n")
            
    output_report_path = data_dir / "auto_rater_summarizer_report.md"
    with open(output_report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(markdown_lines))
        
    logger.info("Successfully compiled Summarization Quality Report to %s", output_report_path)
    print("\n--- Quality Report Saved ---\n")

if __name__ == "__main__":
    main()
