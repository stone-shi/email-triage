#!/usr/bin/env python3
import json
import logging
import argparse
import yaml
import sys
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import httpx

from config import settings
from triage import EmailTriageEngine
from db import EmailDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] offline_eval: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("offline_eval")

def extract_json(text: str) -> str:
    import re
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    
    # Robustness fix: handle unquoted tags from lazy models
    text = re.sub(r'("tag":\s*)(?!(?:true|false|null)\b)([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*[,}])', r'\1"\2"', text)
    
    # Robustness fix: handle invalid escapes like \'
    text = text.replace("\\'", "'")
    
    return text

def get_filtered_emails(db: EmailDB, days: Optional[int], start_date: Optional[str], end_date: Optional[str]) -> List[Dict[str, Any]]:
    """Retrieve cached email records from SQLite filtered by timeframe."""
    query = "SELECT * FROM email_cache"
    params = []
    conditions = []

    if days is not None:
        time_threshold = (datetime.utcnow() - timedelta(days=days)).isoformat()
        conditions.append("processed_at >= ?")
        params.append(time_threshold)
    
    if start_date is not None:
        conditions.append("date(processed_at) >= ?")
        params.append(start_date)
        
    if end_date is not None:
        conditions.append("date(processed_at) <= ?")
        params.append(end_date)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
        
    query += " ORDER BY processed_at DESC"

    try:
        with db._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error("Failed to query email_cache database: %s", e)
        return []

def run_baseline_triage(
    engine: EmailTriageEngine,
    email: Dict[str, Any],
    config: Dict[str, Any],
    base_url: str,
    headers: Dict[str, str],
    prompts: Dict[str, Any]
) -> Dict[str, Any]:
    """Simulate the triage pipeline using the baseline configuration."""
    sender = email.get("sender") or ""
    subject = email.get("subject") or ""
    email_body = email.get("email_body") or ""
    snippet = email.get("snippet") or email_body[:200]
    msg_id = email.get("message_id")

    triage_model = config.get("triage_model", settings.triage_model)
    summary_model = config.get("summary_model", settings.summary_model)
    http_client = engine.http_client

    result = {
        "message_id": msg_id,
        "triage_level": 0,
        "reason": "Passed static filter",
        "score": 1.0,
        "tag": "notification",
        "summary": None,
        "tei_enabled": config.get("tei_router_enabled", False),
        "tei_score": None,
        "tei_decision": None,
        "level_1_run": False,
        "level_1_model": triage_model,
        "level_1_score": None,
        "level_2_run": False,
        "level_2_model": summary_model,
        "duration_sec": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0
    }

    start_time = time.time()

    # 1. VIP Whitelist check
    if engine.is_vip_sender(sender):
        result["triage_level"] = 2
        result["reason"] = "VIP Whitelist direct escalation"
        result["tag"] = "vip"
        
        if not email_body or len(email_body.strip()) < 10:
            result["summary"] = "No substantive content to summarize."
        else:
            # Call baseline Level 2 Summarization
            result["level_2_run"] = True
            l2_system = prompts.get("level_2_summarization", {}).get("system", "")
            l2_prompt = f"Subject: {subject}\nBody:\n{email_body[:8000]}"
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
                resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                resp.raise_for_status()
                resp_json = resp.json()
                
                usage = resp_json.get("usage", {})
                result["prompt_tokens"] += usage.get("prompt_tokens", 0)
                result["completion_tokens"] += usage.get("completion_tokens", 0)
                
                content = resp_json["choices"][0]["message"]["content"]
                res_dict = json.loads(extract_json(content))
                result["summary"] = res_dict.get("summary", "")
                result["score"] = res_dict.get("confidence_score", 1.0)
                result["tag"] = res_dict.get("tag", "vip")
            except Exception as e:
                result["summary"] = f"Summary generation failed: {e}"
        
        result["duration_sec"] = time.time() - start_time
        return result

    # 2. Level 0 Static filter
    is_noise, l0_reason = engine.run_level_0_static(sender, subject)
    if is_noise:
        result["triage_level"] = 0
        result["reason"] = l0_reason
        result["tag"] = "low"
        result["duration_sec"] = time.time() - start_time
        return result

    # 3. Level 0.5 TEI Semantic Router
    if config.get("tei_router_enabled", False):
        tei_override_level, tei_reason, tei_score = engine.run_tei_router(sender, subject, snippet)
        result["tei_score"] = tei_score
        
        if tei_override_level == 0:
            result["triage_level"] = 0
            result["reason"] = tei_reason
            result["score"] = tei_score
            result["tag"] = "low"
            result["tei_decision"] = "noise"
            result["duration_sec"] = time.time() - start_time
            return result
        elif tei_override_level == 2:
            result["triage_level"] = 2
            result["reason"] = tei_reason
            result["score"] = tei_score
            result["tei_decision"] = "signal"
            
            if not email_body or len(email_body.strip()) < 10:
                result["summary"] = "No substantive content to summarize."
            else:
                result["level_2_run"] = True
                l2_system = prompts.get("level_2_summarization", {}).get("system", "")
                l2_prompt = f"Subject: {subject}\nBody:\n{email_body[:8000]}"
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
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                    resp.raise_for_status()
                    resp_json = resp.json()
                    
                    usage = resp_json.get("usage", {})
                    result["prompt_tokens"] += usage.get("prompt_tokens", 0)
                    result["completion_tokens"] += usage.get("completion_tokens", 0)
                    
                    content = resp_json["choices"][0]["message"]["content"]
                    res_dict = json.loads(extract_json(content))
                    result["summary"] = res_dict.get("summary", "")
                    result["score"] = res_dict.get("confidence_score", tei_score)
                    result["tag"] = res_dict.get("tag", "notification")
                except Exception as e:
                    result["summary"] = f"Summary generation failed: {e}"
            
            result["duration_sec"] = time.time() - start_time
            return result
        else:
            result["tei_decision"] = "neutral"

    # 4. Level 1 Classification
    result["level_1_run"] = True
    
    # If baseline specifies a different triage type (e.g., TEI classifier sequence)
    if config.get("triage_type", "llm") == "tei":
        tei_url = config.get("tei_url", "http://10.100.0.50:8077/predict")
        tei_text = f"From: {sender} | Subject: {subject} | Snippet: {snippet}"
        try:
            resp = http_client.post(tei_url, json={"inputs": tei_text})
            resp.raise_for_status()
            predictions = resp.json()
            winning_pred = max(predictions, key=lambda x: x.get("score", 0.0))
            winning_label = winning_pred.get("label", "").lower()
            winning_score = winning_pred.get("score", 1.0)
            is_important = ("entailment" in winning_label and "not_" not in winning_label) or "important" in winning_label
            
            suggested_level = 2 if is_important else 1
            reason = f"TEI Classifier resolved winning label: '{winning_label}'"
            tag = "notification" if not is_important else "personal"
            
            result["triage_level"] = suggested_level
            result["reason"] = reason
            result["score"] = winning_score
            result["tag"] = tag
            result["level_1_score"] = winning_score
        except Exception as e:
            result["triage_level"] = 2
            result["reason"] = f"TEI Classifier prediction failed: {e}"
            result["score"] = 1.0
            result["tag"] = "personal"
            result["level_1_score"] = 1.0
    else:
        # LiteLLM / DeepSeek level 1 call
        l1_system = prompts.get("level_1_fast_triage", {}).get("system", "")
        l1_prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
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
            resp_json = resp.json()
            
            usage = resp_json.get("usage", {})
            result["prompt_tokens"] += usage.get("prompt_tokens", 0)
            result["completion_tokens"] += usage.get("completion_tokens", 0)
            
            content = resp_json["choices"][0]["message"]["content"]
            res_dict = json.loads(extract_json(content))
            
            result["triage_level"] = res_dict.get("suggested_level", 1)
            result["reason"] = res_dict.get("reason", "")
            result["score"] = res_dict.get("confidence_score", 1.0)
            result["tag"] = res_dict.get("tag", "notification")
            result["level_1_score"] = res_dict.get("confidence_score", 1.0)
        except Exception as e:
            result["triage_level"] = 1
            result["reason"] = f"Level 1 classification failed: {e}"
            result["score"] = 0.0
            result["tag"] = "notification"
            result["level_1_score"] = 0.0

    # 5. Level 2 Summarization (Only if Level 1 suggested Level 2)
    if result["triage_level"] == 2:
        if not email_body or len(email_body.strip()) < 10:
            result["summary"] = "No substantive content to summarize."
        else:
            result["level_2_run"] = True
            l2_system = prompts.get("level_2_summarization", {}).get("system", "")
            l2_prompt = f"Subject: {subject}\nBody:\n{email_body[:8000]}"
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
                resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                resp.raise_for_status()
                resp_json = resp.json()
                
                usage = resp_json.get("usage", {})
                result["prompt_tokens"] += usage.get("prompt_tokens", 0)
                result["completion_tokens"] += usage.get("completion_tokens", 0)
                
                content = resp_json["choices"][0]["message"]["content"]
                res_dict = json.loads(extract_json(content))
                
                result["summary"] = res_dict.get("summary", "")
                result["score"] = res_dict.get("confidence_score", result["score"])
                result["tag"] = res_dict.get("tag", result["tag"])
            except Exception as e:
                result["summary"] = f"Level 2 summarization failed: {e}"

    result["duration_sec"] = time.time() - start_time
    return result

def evaluate_summary_quality(
    http_client: httpx.Client,
    base_url: str,
    headers: Dict[str, str],
    judge_model: str,
    subject: str,
    body: str,
    prod_summary: str,
    base_summary: str
) -> Dict[str, Any]:
    """Call the LLM Judge to rate and compare two summaries on a 1-10 scale."""
    judge_system = (
        "You are an expert quality controller evaluating summary accuracy, conciseness, and fidelity.\n"
        "You are given an original email and two generated summaries (Summary A and Summary B).\n"
        "Rate both summaries on a scale of 1 to 10 (where 10 is perfect) and provide a winner ('A', 'B', or 'Tie').\n"
        "You MUST return a valid JSON object containing exactly the following fields:\n"
        "- 'score_a': float (0.0 to 10.0)\n"
        "- 'score_b': float (0.0 to 10.0)\n"
        "- 'winner': string ('A', 'B', or 'Tie')\n"
        "- 'reasoning': string explaining the scores and choice."
    )
    
    judge_prompt = (
        f"Subject: {subject}\n"
        f"Full Email Body:\n{body[:4000]}\n\n"
        f"--- Summary A ---\n{prod_summary}\n\n"
        f"--- Summary B ---\n{base_summary}\n\n"
        "Compare Summary A (Production) vs Summary B (Baseline)."
    )
    
    try:
        payload = {
            "model": judge_model,
            "messages": [
                {"role": "system", "content": judge_system},
                {"role": "user", "content": judge_prompt}
            ],
            "temperature": 0.0
        }
        resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(extract_json(content))
    except Exception as e:
        logger.error("LLM Judge failed to evaluate summary quality: %s", e)
        return {
            "score_a": 0.0,
            "score_b": 0.0,
            "winner": "Error",
            "reasoning": f"Judge evaluation failed: {e}"
        }

def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Quality of Service (QoS) Evaluation Runner")
    parser.add_argument("--baseline", type=str, required=True, help="Target baseline profile name from auto_rater_config.yml")
    parser.add_argument("--days", type=int, help="Number of past days of cached emails to process")
    parser.add_argument("--start-date", type=str, help="Start date in YYYY-MM-DD format")
    parser.add_argument("--end-date", type=str, help="End date in YYYY-MM-DD format")
    parser.add_argument("--max-items", type=int, help="Limit the number of evaluated cached items")
    parser.add_argument("--judge", action="store_true", help="Enable LLM judge rating for summary comparisons")
    args = parser.parse_args()

    workspace_dir = Path(__file__).parent.resolve()
    db = EmailDB(db_path=workspace_dir / "email_cache.db")
    engine = EmailTriageEngine(db)

    # 1. Load Configurations
    config_path = workspace_dir / "auto_rater_config.yml"
    if not config_path.exists():
        logger.error("Missing auto_rater_config.yml configuration file.")
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    test_configs = config_data.get("test_configurations", [])
    baseline_config = next((cfg for cfg in test_configs if cfg.get("name") == args.baseline), None)
    
    if not baseline_config:
        logger.error("Could not find baseline configuration profile matching: '%s'", args.baseline)
        sys.exit(1)
        
    judge_model = config_data.get("judge_model", "gemini/gemini-3.1-pro-preview")

    # Load prompts
    prompts_path = workspace_dir / "prompts.yml"
    prompts = {}
    try:
        if prompts_path.exists():
            with open(prompts_path, "r", encoding="utf-8") as f:
                prompts = yaml.safe_load(f) or {}
    except Exception:
        pass

    # 2. Retrieve Filtered Cached Emails
    emails = get_filtered_emails(db, args.days, args.start_date, args.end_date)
    if not emails:
        logger.info("No cached emails found matching timeframe filters.")
        sys.exit(0)
        
    if args.max_items is not None:
        emails = emails[:args.max_items]
        
    logger.info("Loaded %d emails from local cache for offline evaluation.", len(emails))

    # 3. Execute Baseline Simulation & Comparisons
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}"
    }

    comparisons: List[Dict[str, Any]] = []
    
    agree_triage = 0
    agree_important = 0
    disagree_triage = 0
    missing_body_warnings = 0
    
    # Standard 2x2 Confusion Matrix: Production (Actual) vs. Baseline (Predicted)
    # Positives = Important (Level 2)
    # Negatives = Low/Unimportant (Level 0 or 1)
    tp, fp, fn, tn = 0, 0, 0, 0
    
    total_prod_tokens = 0
    total_base_tokens = 0
    total_prod_time = 0.0
    total_base_time = 0.0

    logger.info("Starting offline pipeline runs...")
    for idx, email in enumerate(emails, 1):
        msg_id = email["message_id"]
        logger.info("[%d/%d] Evaluating message: '%s' (Sender: %s)", idx, len(emails), email.get("subject", ""), email.get("sender", ""))

        # Run baseline
        try:
            base_res = run_baseline_triage(engine, email, baseline_config, base_url, headers, prompts)
        except Exception as run_err:
            logger.error("Baseline run failed for email %s: %s", msg_id, run_err)
            continue

        # Read Production results stored in cache
        prod_level = email.get("triage_level", 1)
        if prod_level is None:
            prod_level = 0 if email.get("level_0_status") == "filtered" else (2 if email.get("level_1_status") == "important" else 1)
            
        prod_summary = email.get("level_2_summary")
        prod_reason = email.get("reason")
        prod_score = email.get("score") or 1.0
        prod_tag = email.get("tag") or "notification"
        
        # Sum up tokens & latency (Production)
        l1_prompt = email.get("level_1_prompt_tokens") or 0
        l1_comp = email.get("level_1_completion_tokens") or 0
        l2_prompt = email.get("level_2_prompt_tokens") or 0
        l2_comp = email.get("level_2_completion_tokens") or 0
        total_prod_tokens += (l1_prompt + l1_comp + l2_prompt + l2_comp)
        
        l1_dur = email.get("level_1_duration_sec") or 0.0
        l2_dur = email.get("level_2_duration_sec") or 0.0
        total_prod_time += (l1_dur + l2_dur)
        
        # Sum up tokens & latency (Baseline)
        total_base_tokens += (base_res["prompt_tokens"] + base_res["completion_tokens"])
        total_base_time += base_res["duration_sec"]

        # Evaluate summary quality if both ran level 2 summaries
        judge_metrics = None
        if args.judge and prod_summary and base_res["summary"] and email.get("email_body"):
            logger.info("Calling LLM Judge to evaluate summary quality differential...")
            judge_metrics = evaluate_summary_quality(
                engine.http_client, base_url, headers, judge_model,
                email.get("subject", ""), email.get("email_body", ""),
                prod_summary, base_res["summary"]
            )

        # Alignment tracking
        prod_important = (prod_level == 2)
        base_important = (base_res["triage_level"] == 2)
        
        if prod_important and base_important:
            tp += 1
            agree_important += 1
        elif not prod_important and base_important:
            fp += 1
        elif prod_important and not base_important:
            fn += 1
        else:
            tn += 1

        if prod_level == base_res["triage_level"]:
            agree_triage += 1
        else:
            disagree_triage += 1
            
        if base_important and not email.get("email_body"):
            missing_body_warnings += 1
            logger.warning("⚠️ Email %s was escalated to Level 2 by baseline, but email_body was missing in cache!", msg_id)

        comparisons.append({
            "message_id": msg_id,
            "sender": email.get("sender"),
            "subject": email.get("subject"),
            "date": email.get("date_str"),
            "production": {
                "triage_level": prod_level,
                "reason": prod_reason,
                "score": prod_score,
                "tag": prod_tag,
                "summary": prod_summary,
                "total_tokens": l1_prompt + l1_comp + l2_prompt + l2_comp,
                "duration_sec": l1_dur + l2_dur
            },
            "baseline": {
                "triage_level": base_res["triage_level"],
                "reason": base_res["reason"],
                "score": base_res["score"],
                "tag": base_res["tag"],
                "summary": base_res["summary"],
                "total_tokens": base_res["prompt_tokens"] + base_res["completion_tokens"],
                "duration_sec": base_res["duration_sec"]
            },
            "judge": judge_metrics
        })

    # 4. Compute Summary Metrics
    total_count = len(comparisons)
    triage_agreement_rate = agree_triage / total_count if total_count > 0 else 0.0
    
    # Classical metrics regarding 'Important' identification
    accuracy = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    
    # Average latency/costs
    avg_prod_time = total_prod_time / total_count if total_count > 0 else 0.0
    avg_base_time = total_base_time / total_count if total_count > 0 else 0.0
    avg_prod_tokens = total_prod_tokens / total_count if total_count > 0 else 0.0
    avg_base_tokens = total_base_tokens / total_count if total_count > 0 else 0.0

    # 5. Generate Report
    report_lines = []
    report_lines.append(f"# 📊 QoS Offline Evaluation: Production vs. `{args.baseline}`")
    report_lines.append(f"Evaluation run generated on: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**\n")
    
    report_lines.append("## ⚙️ Configuration Analytics Summary")
    report_lines.append(f"- **Evaluation Scope**: {total_count} cached emails")
    if args.days:
        report_lines.append(f"- **Timeframe Filter**: Past {args.days} days")
    elif args.start_date or args.end_date:
        report_lines.append(f"- **Timeframe Filter**: {args.start_date or 'Start'} to {args.end_date or 'End'}")
    report_lines.append(f"- **Active Production Triage Model**: `{settings.triage_model}`")
    report_lines.append(f"- **Active Production Summary Model**: `{settings.summary_model}`")
    report_lines.append(f"- **Baseline Configuration Profile**: `{args.baseline}` (Triage: `{baseline_config.get('triage_model')}`, Summary: `{baseline_config.get('summary_model')}`)")
    report_lines.append(f"- **Total Missing Cache Body Warnings**: {missing_body_warnings}")
    report_lines.append("")
    
    report_lines.append("## 🎯 Priority & Triage Decision Alignment")
    report_lines.append(f"- **Exact Triage Level Agreement**: {triage_agreement_rate * 100:.1f}% ({agree_triage} of {total_count})")
    report_lines.append(f"- **Total Classifications Disagreed**: {disagree_triage}")
    report_lines.append("")
    
    report_lines.append("### 📉 Signal vs. Noise Matrix (Production as Actual, Baseline as Predicted)")
    report_lines.append("| | Predicted Unimportant (L0/L1) | Predicted Important (L2) |")
    report_lines.append("|---|---|---|")
    report_lines.append(f"| **Actual Unimportant (L0/L1)** | True Negative: **{tn}** | False Positive: **{fp}** |")
    report_lines.append(f"| **Actual Important (L2)** | False Negative: **{fn}** | True Positive: **{tp}** |")
    report_lines.append("")
    
    report_lines.append("### 📐 Statistical Alignment Metrics")
    report_lines.append(f"- **Relative Alignment Accuracy**: {accuracy * 100:.1f}%")
    report_lines.append(f"- **Relative Alignment Precision**: {precision * 100:.1f}%")
    report_lines.append(f"- **Relative Alignment Recall**: {recall * 100:.1f}%")
    report_lines.append(f"- **F1-Score Alignment**: {f1:.3f}")
    report_lines.append("")
    
    report_lines.append("## ⚡ Cost & Performance Telemetry Comparisons")
    report_lines.append("| Telemetry Parameter | Active Production | Baseline Configuration | Differential |")
    report_lines.append("|---|---|---|---|")
    report_lines.append(f"| **Avg Latency (Sec/Email)** | {avg_prod_time:.3f}s | {avg_base_time:.3f}s | {avg_base_time - avg_prod_time:+.3f}s |")
    report_lines.append(f"| **Avg Tokens (Tokens/Email)** | {avg_prod_tokens:.1f} | {avg_base_tokens:.1f} | {avg_base_tokens - avg_prod_tokens:+.1f} |")
    report_lines.append("")

    if args.judge:
        report_lines.append("## ⚖️ Summary Quality judge Rating Analytics")
        winning_a = sum(1 for c in comparisons if c["judge"] and c["judge"].get("winner") == "A")
        winning_b = sum(1 for c in comparisons if c["judge"] and c["judge"].get("winner") == "B")
        ties = sum(1 for c in comparisons if c["judge"] and c["judge"].get("winner") == "Tie")
        rated_total = winning_a + winning_b + ties
        
        if rated_total > 0:
            avg_score_a = sum(c["judge"].get("score_a", 0.0) for c in comparisons if c["judge"]) / rated_total
            avg_score_b = sum(c["judge"].get("score_b", 0.0) for c in comparisons if c["judge"]) / rated_total
            
            report_lines.append(f"- **Total Summaries Rated**: {rated_total}")
            report_lines.append(f"- **Average Score Production (A)**: **{avg_score_a:.2f}/10.0**")
            report_lines.append(f"- **Average Score Baseline (B)**: **{avg_score_b:.2f}/10.0**")
            report_lines.append(f"- **Production Wins**: {winning_a} ({winning_a/rated_total*100:.1f}%)")
            report_lines.append(f"- **Baseline Wins**: {winning_b} ({winning_b/rated_total*100:.1f}%)")
            report_lines.append(f"- **Ties**: {ties} ({ties/rated_total*100:.1f}%)")
        else:
            report_lines.append("No overlapping level 2 summaries found to run the LLM judge evaluation.")
        report_lines.append("")

    report_lines.append("## 📝 Granular Triage Disagreement Breakdown")
    report_lines.append("| Message ID | Sender | Subject | Prod Level (Tag) | Base Level (Tag) | Prod Reason | Base Reason |")
    report_lines.append("|---|---|---|---|---|---|---|")
    
    disagree_rows = 0
    for c in comparisons:
        if c["production"]["triage_level"] != c["baseline"]["triage_level"]:
            disagree_rows += 1
            # Shorten variables to display nicely in markdown table
            subject_short = c["subject"][:40] + "..." if len(c["subject"]) > 40 else c["subject"]
            sender_short = c["sender"][:30] + "..." if len(c["sender"]) > 30 else c["sender"]
            prod_reason_short = c["production"]["reason"][:50] + "..." if c["production"]["reason"] and len(c["production"]["reason"]) > 50 else (c["production"]["reason"] or "")
            base_reason_short = c["baseline"]["reason"][:50] + "..." if c["baseline"]["reason"] and len(c["baseline"]["reason"]) > 50 else (c["baseline"]["reason"] or "")
            
            report_lines.append(
                f"| `{c['message_id'][:8]}...` | {sender_short} | {subject_short} | "
                f"{c['production']['triage_level']} ({c['production']['tag']}) | "
                f"{c['baseline']['triage_level']} ({c['baseline']['tag']}) | "
                f"{prod_reason_short} | {base_reason_short} |"
            )
            
    if disagree_rows == 0:
        report_lines.append("| - | - | No disagreements recorded! Perfect alignment! | - | - | - | - |")
    report_lines.append("")

    # Save report to disk
    output_report_path = workspace_dir / "auto_rater_data" / f"offline_eval_report_{args.baseline}.md"
    output_report_path.parent.mkdir(exist_ok=True)
    with open(output_report_path, "w", encoding="utf-8") as rep_f:
        rep_f.write("\n".join(report_lines))

    # Save granular JSON comparisons for audit
    output_json_path = workspace_dir / "auto_rater_data" / f"offline_eval_results_{args.baseline}.json"
    with open(output_json_path, "w", encoding="utf-8") as js_f:
        json.dump({
            "baseline_profile": args.baseline,
            "triage_agreement_rate": triage_agreement_rate,
            "alignment_f1_score": f1,
            "telemetry": {
                "avg_prod_time": avg_prod_time,
                "avg_base_time": avg_base_time,
                "avg_prod_tokens": avg_prod_tokens,
                "avg_base_tokens": avg_base_tokens
            },
            "comparisons": comparisons
        }, js_f, indent=2, ensure_ascii=False)

    # Print summary output to stdout
    print("\n==================================================")
    print("🏁 OFFLINE QoS EVALUATION COMPLETION METRICS SUMMARY")
    print("==================================================")
    print(f"Scope Evaluated: {total_count} emails")
    print(f"Exact Triage Level Alignment: {triage_agreement_rate*100:.1f}% ({agree_triage}/{total_count})")
    print(f"Relative Important Identification Alignment:")
    print(f"  - Accuracy:  {accuracy*100:.1f}%")
    print(f"  - Precision: {precision*100:.1f}%")
    print(f"  - Recall:    {recall*100:.1f}%")
    print(f"  - F1-Score:  {f1:.3f}")
    print("Telemetry:")
    print(f"  - Prod Latency:  {avg_prod_time:.3f}s/email | Base Latency:  {avg_base_time:.3f}s/email")
    print(f"  - Prod Token Cost: {avg_prod_tokens:.1f}/email    | Base Token Cost: {avg_base_tokens:.1f}/email")
    print("==================================================")
    print(f"📝 Complete report saved pretty to: {output_report_path}")
    print(f"⚙️ Granular raw results saved to:   {output_json_path}\n")

if __name__ == "__main__":
    main()
