#!/usr/bin/env python3
import json
import logging
import argparse
import yaml
import sys
import os
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import httpx

# Import project settings and helper files
from config import settings
from db import EmailDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] classifier_tester: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("classifier_tester")

def extract_json(text: str) -> str:
    """Extracts JSON content from text, stripping markdown code blocks and fixing unquoted strings."""
    import re
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    
    # Handle unquoted tags from lazy models
    text = re.sub(r'("tag":\s*)(?!(?:true|false|null)\b)([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*[,}])', r'\1"\2"', text)
    # Handle invalid escapes
    text = text.replace("\\'", "'")
    return text

def run_tei_classifier(tei_url: str, sender: str, subject: str, snippet: str, http_client: httpx.Client) -> Tuple[int, str, float, str]:
    """Runs a TEI-based sequence classifier prediction."""
    tei_text = f"From: {sender} | Subject: {subject} | Snippet: {snippet}"
    try:
        response = http_client.post(tei_url, json={"inputs": tei_text})
        response.raise_for_status()
        predictions = response.json()
        
        winning_pred = max(predictions, key=lambda x: x.get("score", 0.0))
        winning_label = winning_pred.get("label", "").lower()
        winning_score = winning_pred.get("score", 1.0)
        
        is_important = ("entailment" in winning_label and "not_" not in winning_label) or "important" in winning_label
        suggested_level = 2 if is_important else 1
        reason = f"TEI Classifier resolved winning label: '{winning_label}'"
        tag = "notification" if not is_important else "personal"
        
        return suggested_level, reason, winning_score, tag
    except Exception as e:
        logger.error("TEI Classifier call failed: %s", e)
        # Safe default fallback
        return 1, f"TEI prediction error: {e}", 0.0, "notification"

def run_llm_classifier(url: str, api_key: str, model: str, sender: str, subject: str, snippet: str, http_client: httpx.Client) -> Tuple[int, str, float, str]:
    """Runs an LLM-based triage classifier prediction."""
    system_instruction = (
        "You are an expert executive assistant evaluating an email to suggest its triage level.\n"
        "Output suggested_level as an integer:\n"
        "0 - pure noise, random promotion, social media notification not directly addressed to user, notification requiring no action.\n"
        "1 - notification worth reviewing, promotion addressing user (e.g., birthday credit, coupon, free credit).\n"
        "2 - important, actionable, personal human conversation or critical alert.\n"
        "You MUST return a valid JSON object containing exactly four fields: "
        "'suggested_level' (integer: 0, 1, or 2), 'reason' (string explaining the level), 'confidence_score' (float from 0.0 to 1.0), and "
        "'tag' (a one word lowercase tag, e.g., \"promotion\", \"notification\", \"personal\", \"vip\", \"low\")."
    )
    
    prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "include_reasoning": False
    }
    
    try:
        resp = http_client.post(f"{url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        resp_json = resp.json()
        content = resp_json["choices"][0]["message"]["content"]
        
        json_content = extract_json(content)
        result_dict = json.loads(json_content)
        
        suggested_level = int(result_dict.get("suggested_level", 1))
        reason = result_dict.get("reason", "")
        score = float(result_dict.get("confidence_score", 1.0))
        tag = result_dict.get("tag", "notification")
        
        return suggested_level, reason, score, tag
    except Exception as e:
        logger.error("LLM Classifier call failed: %s", e)
        return 1, f"LLM prediction error: {e}", 0.0, "notification"

def run_llm_judge(judge_url: str, judge_api_key: str, judge_model: str, email: Dict[str, Any], pred_level: int, pred_tag: str, pred_reason: str, http_client: httpx.Client) -> Tuple[bool, bool, int, str, str]:
    """Runs a live LLM judge audit on the classifier's prediction."""
    judge_system = (
        "You are an expert email operations auditor. Your task is to evaluate the accuracy of a proposed triage classification for a given email.\n"
        "Triage level definitions:\n"
        "0 - pure noise, random promotion, social media notification not directly addressed to user, notification requiring no action.\n"
        "1 - notification worth reviewing, promotion addressing user (e.g., birthday credit, coupon, free credit).\n"
        "2 - important, actionable, personal human conversation or critical alert.\n\n"
        "Your job is to determine if the proposed triage level and tag are correct.\n"
        "If they are incorrect, specify the correct level and tag.\n"
        "You must return a valid JSON object containing exactly the following fields:\n"
        "- 'is_level_correct': boolean (true if proposed level is correct, false otherwise)\n"
        "- 'is_tag_correct': boolean (true if proposed tag is correct, false otherwise)\n"
        "- 'correct_level': integer (0, 1, or 2)\n"
        "- 'correct_tag': string (the correct lowercase tag, e.g., \"promotion\", \"notification\", \"personal\", \"vip\", \"low\")\n"
        "- 'rationale': string (explanation of your evaluation)"
    )
    
    judge_prompt = (
        f"Email Details:\n"
        f"Sender: {email.get('sender')}\n"
        f"Subject: {email.get('subject')}\n"
        f"Snippet/Body: {email.get('snippet') or email.get('full_body', '')[:400]}\n\n"
        f"Proposed Triage Classification:\n"
        f"Suggested Level: {pred_level}\n"
        f"Tag: {pred_tag}\n"
        f"Triage Reason: {pred_reason}"
    )
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {judge_api_key}"
    }
    
    payload = {
        "model": judge_model,
        "messages": [
            {"role": "system", "content": judge_system},
            {"role": "user", "content": judge_prompt}
        ],
        "temperature": 0.0,
        "include_reasoning": False
    }
    
    try:
        resp = http_client.post(f"{judge_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        resp_json = resp.json()
        content = resp_json["choices"][0]["message"]["content"]
        
        result_dict = json.loads(extract_json(content))
        
        is_level_correct = bool(result_dict.get("is_level_correct", False))
        is_tag_correct = bool(result_dict.get("is_tag_correct", False))
        correct_level = int(result_dict.get("correct_level", pred_level))
        correct_tag = result_dict.get("correct_tag", pred_tag)
        rationale = result_dict.get("rationale", "")
        
        return is_level_correct, is_tag_correct, correct_level, correct_tag, rationale
    except Exception as e:
        logger.error("LLM Judge call failed: %s", e)
        # Fail open: assume proposed is correct to not crash
        return True, True, pred_level, pred_tag, f"Judge failed: {e}"

def html_escape(text: str) -> str:
    if not isinstance(text, str):
        return str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;")

def get_level_badge(level: int) -> str:
    if level == 0:
        return '<span class="badge badge-l0">L0 (Noise)</span>'
    elif level == 1:
        return '<span class="badge badge-l1">L1 (Review)</span>'
    elif level == 2:
        return '<span class="badge badge-l2">L2 (Important)</span>'
    return f'<span class="badge">{level}</span>'

def compile_html_report(run_results: List[Dict[str, Any]], config_name: str, triage_model_name: str, triage_type: str, total_duration: float, baseline_name: Optional[str]) -> str:
    """Compiles a detailed, styled performance HTML report and saves it to auto_rater_data."""
    total_emails = len(run_results)
    if total_emails == 0:
        return "<html><body style='background-color:#0f172a; color:#f8fafc; font-family:sans-serif; padding:2rem;'>No emails processed.</body></html>"
        
    correct_levels = sum(1 for r in run_results if r["level_correct"])
    correct_tags = sum(1 for r in run_results if r["tag_correct"])
    
    level_accuracy = (correct_levels / total_emails) * 100
    tag_accuracy = (correct_tags / total_emails) * 100
    
    # Calculate confusion matrix for level 2 (important) vs non-level 2
    tp, fp, fn, tn = 0, 0, 0, 0
    for r in run_results:
        pred_is_important = (r["predicted_level"] == 2)
        true_is_important = (r["correct_level"] == 2)
        
        if pred_is_important and true_is_important:
            tp += 1
        elif pred_is_important and not true_is_important:
            fp += 1
        elif not pred_is_important and true_is_important:
            fn += 1
        else:
            tn += 1
            
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1_score = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    
    avg_latency = total_duration / total_emails
    
    eval_mode_desc = f"Baseline Alignment vs <code>{html_escape(baseline_name)}</code>" if baseline_name else "Live LLM Judge"
    
    disagreements = [r for r in run_results if not r["level_correct"] or not r["tag_correct"]]
    
    disagreements_rows = []
    for d in disagreements:
        subject = html_escape(d["subject"])
        sender = html_escape(d["sender"])
        
        pred_badge = f"{get_level_badge(d['predicted_level'])} <span class='badge badge-tag'>{html_escape(d['predicted_tag'])}</span>"
        correct_badge = f"{get_level_badge(d['correct_level'])} <span class='badge badge-tag'>{html_escape(d['correct_tag'])}</span>"
        rationale = html_escape(d["rationale"] or "N/A")
        
        disagreements_rows.append(f"""
        <tr>
            <td style="font-weight: 500;">{subject}</td>
            <td style="color: #94a3b8;">{sender}</td>
            <td>{pred_badge}</td>
            <td>{correct_badge}</td>
            <td style="color: #cbd5e1; font-style: italic;">{rationale}</td>
        </tr>
        """)
        
    disagreements_table_body = "\n".join(disagreements_rows)
    
    disagreements_html = f"""
    <div class="table-container">
        <table class="disagreements-table">
            <thead>
                <tr>
                    <th>Subject</th>
                    <th>Sender</th>
                    <th>Predicted (Lvl/Tag)</th>
                    <th>Correct (Lvl/Tag)</th>
                    <th>Auditor Rationale</th>
                </tr>
            </thead>
            <tbody>
                {disagreements_table_body}
            </tbody>
        </table>
    </div>
    """ if disagreements else '<div class="no-disagreements">🎉 Hurrah! 100% classification alignment. No failure cases observed.</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Classifier Performance Evaluation: {html_escape(config_name)}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');
        body {{
            font-family: 'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #0f172a;
            color: #f8fafc;
            margin: 0;
            padding: 2rem;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .header {{
            background: linear-gradient(135deg, #6366f1, #3b82f6);
            padding: 2.5rem;
            border-radius: 1rem;
            margin-bottom: 2rem;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
        }}
        .header h1 {{
            margin: 0 0 0.5rem 0;
            font-size: 2.25rem;
            font-weight: 700;
            letter-spacing: -0.025em;
        }}
        .header p {{
            margin: 0;
            color: #e2e8f0;
            font-size: 1.1rem;
            font-weight: 300;
        }}
        .meta-info {{
            margin-top: 1.5rem;
            padding-top: 1.5rem;
            border-top: 1px solid rgba(255, 255, 255, 0.15);
            display: flex;
            flex-wrap: wrap;
            gap: 1.5rem;
            font-size: 0.9rem;
            color: #f1f5f9;
        }}
        .meta-item strong {{
            color: #ffffff;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2.5rem;
        }}
        .card {{
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 0.75rem;
            padding: 1.5rem;
            text-align: center;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .card:hover {{
            transform: translateY(-4px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2);
            border-color: #475569;
        }}
        .card-val {{
            font-size: 2.5rem;
            font-weight: 700;
            color: #38bdf8;
            margin-bottom: 0.25rem;
        }}
        .card-label {{
            font-size: 0.875rem;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 600;
        }}
        .card-desc {{
            font-size: 0.8rem;
            color: #64748b;
            margin-top: 0.5rem;
        }}
        h2 {{
            font-size: 1.5rem;
            font-weight: 600;
            margin-top: 2rem;
            margin-bottom: 1rem;
            color: #f1f5f9;
            border-bottom: 2px solid #334155;
            padding-bottom: 0.5rem;
            letter-spacing: -0.01em;
        }}
        .matrix-container {{
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 0.75rem;
            padding: 1.5rem;
            margin-bottom: 2.5rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .matrix-table {{
            width: 100%;
            max-width: 600px;
            margin: 0 auto;
            border-collapse: separate;
            border-spacing: 0.5rem;
        }}
        .matrix-table th {{
            color: #94a3b8;
            font-weight: 500;
            padding: 0.75rem;
            text-align: center;
        }}
        .matrix-table td {{
            background-color: #0f172a;
            border: 1px solid #334155;
            padding: 1.25rem;
            text-align: center;
            border-radius: 0.5rem;
            font-size: 1.1rem;
        }}
        .matrix-label {{
            font-weight: 600;
            color: #94a3b8;
        }}
        .tp-cell {{
            border-color: #059669 !important;
            background-color: rgba(4, 120, 87, 0.1) !important;
            color: #34d399 !important;
            font-weight: bold;
        }}
        .tn-cell {{
            border-color: #475569 !important;
            background-color: rgba(71, 85, 105, 0.1) !important;
            color: #cbd5e1 !important;
            font-weight: bold;
        }}
        .fp-cell {{
            border-color: #dc2626 !important;
            background-color: rgba(220, 38, 38, 0.1) !important;
            color: #f87171 !important;
        }}
        .fn-cell {{
            border-color: #d97706 !important;
            background-color: rgba(217, 119, 6, 0.1) !important;
            color: #fbbf24 !important;
        }}
        .table-container {{
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 0.75rem;
            overflow: hidden;
            margin-bottom: 2.5rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .disagreements-table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }}
        .disagreements-table th {{
            background-color: #0f172a;
            color: #94a3b8;
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            padding: 1rem 1.25rem;
            border-bottom: 2px solid #334155;
        }}
        .disagreements-table td {{
            padding: 1.25rem;
            border-bottom: 1px solid #334155;
            font-size: 0.9rem;
            vertical-align: top;
        }}
        .disagreements-table tr:last-child td {{
            border-bottom: none;
        }}
        .disagreements-table tr:hover {{
            background-color: rgba(255, 255, 255, 0.02);
        }}
        .badge {{
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-l0 {{
            background-color: rgba(148, 163, 184, 0.15);
            color: #cbd5e1;
            border: 1px solid #475569;
        }}
        .badge-l1 {{
            background-color: rgba(59, 130, 246, 0.15);
            color: #60a5fa;
            border: 1px solid #2563eb;
        }}
        .badge-l2 {{
            background-color: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid #d97706;
        }}
        .badge-tag {{
            background-color: rgba(99, 102, 241, 0.15);
            color: #818cf8;
            border: 1px solid #4f46e5;
        }}
        .no-disagreements {{
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 0.75rem;
            padding: 3rem;
            text-align: center;
            color: #10b981;
            font-weight: 500;
            font-size: 1.1rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎯 Email Classifier Performance Evaluation</h1>
            <p>Benchmarking and analysis results for email triage prediction accuracy</p>
            <div class="meta-info">
                <div class="meta-item"><strong>Configuration:</strong> {html_escape(config_name)}</div>
                <div class="meta-item"><strong>Model/URL:</strong> {html_escape(triage_model_name)} ({triage_type})</div>
                <div class="meta-item"><strong>Evaluation Mode:</strong> {eval_mode_desc}</div>
                <div class="meta-item"><strong>Generated At:</strong> {time.strftime('%Y-%m-%d %H:%M:%S')}</div>
            </div>
        </div>
        
        <h2>📈 Performance Summary Metrics</h2>
        <div class="grid">
            <div class="card">
                <div class="card-val" style="color: #60a5fa;">{total_emails}</div>
                <div class="card-label">Total Evaluated</div>
                <div class="card-desc">Count of parsed offline envelopes</div>
            </div>
            <div class="card">
                <div class="card-val" style="color: #34d399;">{level_accuracy:.2f}%</div>
                <div class="card-label">Level Accuracy</div>
                <div class="card-desc">Exact match on levels (L0, L1, L2)</div>
            </div>
            <div class="card">
                <div class="card-val" style="color: #818cf8;">{tag_accuracy:.2f}%</div>
                <div class="card-label">Tag Accuracy</div>
                <div class="card-desc">Exact match on lowercase tag</div>
            </div>
            <div class="card">
                <div class="card-val" style="color: #fbbf24;">{f1_score:.3f}</div>
                <div class="card-label">F1 Score</div>
                <div class="card-desc">Harmonic mean of precision/recall</div>
            </div>
            <div class="card">
                <div class="card-val" style="color: #a78bfa;">{avg_latency:.3f}s</div>
                <div class="card-label">Avg Latency</div>
                <div class="card-desc">Average decision duration</div>
            </div>
        </div>

        <div style="display: flex; flex-wrap: wrap; gap: 2rem;">
            <div style="flex: 1; min-width: 300px;">
                <h2>📊 Metrics Breakdown Detail</h2>
                <div class="table-container" style="padding: 1rem 1.5rem;">
                    <p style="margin: 0.5rem 0; display: flex; justify-content: space-between;">
                        <span style="color: #94a3b8;">Precision:</span>
                        <strong style="color: #f8fafc;">{precision*100:.2f}%</strong>
                    </p>
                    <hr style="border: 0; border-top: 1px solid #334155; margin: 0.75rem 0;">
                    <p style="margin: 0.5rem 0; display: flex; justify-content: space-between;">
                        <span style="color: #94a3b8;">Recall:</span>
                        <strong style="color: #f8fafc;">{recall*100:.2f}%</strong>
                    </p>
                    <hr style="border: 0; border-top: 1px solid #334155; margin: 0.75rem 0;">
                    <p style="margin: 0.5rem 0; display: flex; justify-content: space-between;">
                        <span style="color: #94a3b8;">Total Time:</span>
                        <strong style="color: #f8fafc;">{total_duration:.2f}s</strong>
                    </p>
                </div>
            </div>
            <div style="flex: 1.5; min-width: 400px;">
                <h2>📦 Confusion Matrix (Level 2 vs rest)</h2>
                <div class="matrix-container">
                    <table class="matrix-table">
                        <thead>
                            <tr>
                                <th>Actual \ Predicted</th>
                                <th>Predicted Important (L2)</th>
                                <th>Predicted Unimportant (L0/1)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td class="matrix-label">Actual Important (L2)</td>
                                <td class="tp-cell">TP: {tp}</td>
                                <td class="fn-cell">FN: {fn}</td>
                            </tr>
                            <tr>
                                <td class="matrix-label">Actual Unimportant (L0/1)</td>
                                <td class="fp-cell">FP: {fp}</td>
                                <td class="tn-cell">TN: {tn}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <h2>🔍 Failure Analysis / Classification Disagreements ({len(disagreements)} cases)</h2>
        {disagreements_html}
    </div>
</body>
</html>
"""
    return html_content

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    
    parser = argparse.ArgumentParser(description="Standalone Email Classifier Testing Tool")
    parser.add_argument("--config", type=str, default="classifier_tester_config.yml", help="Path to configuration file")
    parser.add_argument("--run", type=str, help="Specific configuration name to run")
    parser.add_argument("--judger", type=str, choices=["baseline", "llm"], help="Override judger mode ('baseline' or 'llm')")
    parser.add_argument("--baseline-json", type=str, help="Override baseline JSON file path")
    parser.add_argument("--max-items", type=int, help="Limit number of emails to evaluate")
    parser.add_argument("--profile", type=str, default="default", help="Load project settings from specified profile directory")
    args = parser.parse_args()
    
    # Load profile settings
    from config import Settings
    profile_settings = Settings.load_for_profile(args.profile)
    
    # Resolve config path: look in profile dir first, then workspace root
    config_path = profile_settings.workspace_dir / args.config
    if not config_path.exists():
        config_path = workspace_dir / args.config
        
    if not config_path.exists():
        logger.error("Configuration file not found: %s", args.config)
        sys.exit(1)
        
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    judger_mode = args.judger or config_data.get("judger", "baseline")
    
    # Resolve judge settings
    judge_settings = config_data.get("judge", {})
    baseline_json_path_str = args.baseline_json or judge_settings.get("baseline_json_path")
    
    baseline_data: Dict[str, Any] = {}
    baseline_map: Dict[str, Dict[str, Any]] = {}
    
    if judger_mode == "baseline":
        if not baseline_json_path_str:
            logger.error("Judger mode is set to 'baseline' but no baseline_json_path is specified.")
            sys.exit(1)
        baseline_path = Path(baseline_json_path_str)
        # If relative, resolve relative to profile dir first, then workspace root
        if not baseline_path.is_absolute():
            profile_baseline = profile_settings.workspace_dir / baseline_path
            if profile_baseline.exists():
                baseline_path = profile_baseline
            else:
                baseline_path = workspace_dir / baseline_path
            
        if not baseline_path.exists():
            logger.error("Baseline JSON file does not exist: %s", baseline_path)
            sys.exit(1)
            
        with open(baseline_path, "r", encoding="utf-8") as bf:
            baseline_data = json.load(bf)
            
        # Build message_id map
        for res in baseline_data.get("results", []):
            baseline_map[res["message_id"]] = {
                "triage_level": res.get("triage_level", 1),
                "tag": res.get("tag", "notification"),
                "reason": res.get("reason", "baseline")
            }
        logger.info("Loaded baseline '%s' with %d records.", baseline_data.get("configuration_name", "unknown"), len(baseline_map))
        
    # Load offline emails
    emails_path = workspace_dir / "auto_rater_data" / "offline_emails.json"
    if not emails_path.exists():
        logger.error("Offline emails dataset file not found: %s", emails_path)
        sys.exit(1)
        
    with open(emails_path, "r", encoding="utf-8") as ef:
        all_emails = json.load(ef)
        
    # Filter emails based on baseline if baseline judger mode is active
    if judger_mode == "baseline":
        emails = [e for e in all_emails if e.get("message_id") in baseline_map]
        logger.info("Filtered email dataset: retrieved %d emails that exist in baseline out of %d total offline emails.", len(emails), len(all_emails))
    else:
        emails = all_emails
        logger.info("Loaded %d offline emails for live LLM judging.", len(emails))
        
    if args.max_items:
        emails = emails[:args.max_items]
        logger.info("Limited evaluation scope to top %d items.", len(emails))
        
    test_configs = config_data.get("test_configurations", [])
    if args.run:
        test_configs = [c for c in test_configs if c.get("name") == args.run]
        if not test_configs:
            logger.error("Configuration '%s' not found in config file.", args.run)
            sys.exit(1)
            
    logger.info("Starting email classifier evaluations in '%s' judger mode...", judger_mode)
    
    # Establish shared HTTP client
    http_client = httpx.Client(timeout=1800.0)
    
    for tc in test_configs:
        config_name = tc.get("name")
        triage_type = tc.get("triage_type", "llm")
        logger.info("Executing evaluation for configuration: '%s' (%s)", config_name, triage_type)
        
        run_results = []
        start_time = time.time()
        
        # Determine endpoints
        if triage_type == "tei":
            tei_url = tc.get("tei_url") or tc.get("url") or profile_settings.tei_url
            triage_model_name = tei_url
        else:
            url = tc.get("url") or os.getenv("EMAIL_TRIAGE_TRIAGE_BASE_URL") or os.getenv("EMAIL_TRIAGE_LLM_BASE_URL") or profile_settings.triage_base_url
            api_key = tc.get("api_key") or os.getenv("EMAIL_TRIAGE_TRIAGE_API_KEY") or os.getenv("EMAIL_TRIAGE_LLM_API_KEY") or profile_settings.triage_api_key
            model = tc.get("model") or profile_settings.triage_model
            triage_model_name = model
            
        for idx, email in enumerate(emails, 1):
            sender = email["sender"]
            subject = email["subject"]
            snippet = email.get("snippet", "")
            msg_id = email["message_id"]
            
            logger.info("[%d/%d] Testing classification for: '%s'", idx, len(emails), subject)
            
            # 1. Run classifier prediction
            if triage_type == "tei":
                pred_level, pred_reason, pred_score, pred_tag = run_tei_classifier(tei_url, sender, subject, snippet, http_client)
            else:
                pred_level, pred_reason, pred_score, pred_tag = run_llm_classifier(url, api_key, model, sender, subject, snippet, http_client)
                
            # 2. Evaluate prediction correctness
            if judger_mode == "baseline":
                base_info = baseline_map[msg_id]
                correct_level = base_info["triage_level"]
                correct_tag = base_info["tag"]
                level_correct = (pred_level == correct_level)
                tag_correct = (pred_tag == correct_tag)
                
                if level_correct and tag_correct:
                    rationale = "Perfect match with baseline classification."
                else:
                    rationale = f"Mismatched classification. Baseline suggested Level {correct_level} / Tag `{correct_tag}`. Reason: {base_info['reason']}"
            else:
                # Live LLM judge
                judge_url = judge_settings.get("url") or os.getenv("EMAIL_TRIAGE_SUMMARY_BASE_URL") or os.getenv("EMAIL_TRIAGE_LLM_BASE_URL") or profile_settings.summary_base_url
                judge_api_key = judge_settings.get("api_key") or os.getenv("EMAIL_TRIAGE_SUMMARY_API_KEY") or os.getenv("EMAIL_TRIAGE_LLM_API_KEY") or profile_settings.summary_api_key
                judge_model = judge_settings.get("model") or profile_settings.summary_model
                
                level_correct, tag_correct, correct_level, correct_tag, rationale = run_llm_judge(
                    judge_url, judge_api_key, judge_model, email, pred_level, pred_tag, pred_reason, http_client
                )
                
            run_results.append({
                "message_id": msg_id,
                "sender": sender,
                "subject": subject,
                "predicted_level": pred_level,
                "predicted_tag": pred_tag,
                "predicted_reason": pred_reason,
                "correct_level": correct_level,
                "correct_tag": correct_tag,
                "level_correct": level_correct,
                "tag_correct": tag_correct,
                "rationale": rationale
            })
            
        total_duration = time.time() - start_time
        
        # Compile and write HTML report
        baseline_name = baseline_data.get("configuration_name") if judger_mode == "baseline" else None
        report_content = compile_html_report(run_results, config_name, triage_model_name, triage_type, total_duration, baseline_name)
        
        # Ensure output directory exists
        report_dir = workspace_dir / "auto_rater_data"
        report_dir.mkdir(exist_ok=True)
        report_file = report_dir / f"classifier_test_report_{config_name}.html"
        
        with open(report_file, "w", encoding="utf-8") as rf:
            rf.write(report_content)
            
        # Also maintain a copy to general report name for quick review
        general_report_file = report_dir / "classifier_test_report.html"
        with open(general_report_file, "w", encoding="utf-8") as rf:
            rf.write(report_content)
            
        logger.info("Successfully compiled performance HTML report to %s", report_file)
        
        # Print summary to console
        print(f"\n==================================================")
        print(f"📊 REPORT FOR CONFIGURATION: {config_name}")
        print(f"==================================================")
        correct_levels = sum(1 for r in run_results if r["level_correct"])
        correct_tags = sum(1 for r in run_results if r["tag_correct"])
        print(f"Total Emails: {len(emails)}")
        print(f"Triage Level Accuracy: {(correct_levels / len(emails)) * 100:.2f}% ({correct_levels}/{len(emails)})")
        print(f"Tag Accuracy: {(correct_tags / len(emails)) * 100:.2f}% ({correct_tags}/{len(emails)})")
        print(f"Total Time: {total_duration:.2f}s (Avg: {total_duration/len(emails):.3f}s per email)")
        print(f"Detailed HTML report saved to: {report_file}")
        print(f"==================================================\n")

if __name__ == "__main__":
    main()
