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
from triage import extract_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_summarizer: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_summarizer")

# Omniroute caches identical requests and serves the hit in ~0.01s. For the judge that's not just
# a latency artifact: entries written by an older code path come back mangled (reasoning prose
# prefixed to the JSON, and the object repeated), which no amount of parser hardening can score.
# Same header (and same load-bearing spelling) as auto_rater_runner.py -- an unrecognized name is
# silently ignored with a 200 still served from cache. This module keeps its own on-disk score
# cache, so bypassing the proxy's cache costs nothing across repeated runs.
OMNIROUTE_NO_CACHE_HEADER = {"X-Omniroute-No-Cache": "true"}

# Ceiling on completion tokens for the judge call. Sized like triage.py's/auto_rater_runner.py's
# constants: big enough that a reasoning judge still reaches its JSON verdict after spending most
# of the budget on hidden thinking tokens, since a mid-thought truncation returns empty content.
MAX_TOKENS_SUMMARY_JUDGE = 3072

# Shared CSS shell -- same look as the other auto_rater_*.py HTML reports, so
# they all read as one family of report.
_HTML_STYLE = """
:root {
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --gridline: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
    --good: #0ca30c; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
    :root {
        color-scheme: dark;
        --surface-1: #1a1a19; --page: #0d0d0d;
        --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
        --gridline: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
        --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
        --good: #0ca30c; --critical: #e66767;
    }
}
* { box-sizing: border-box; }
body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--text-primary);
    margin: 0; padding: 2rem; line-height: 1.5;
}
.container { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.2rem; margin-top: 2.5rem; margin-bottom: 0.75rem; }
p.subtitle { color: var(--text-secondary); margin-top: 0; }
.muted { color: var(--text-secondary); }
.tiles { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.tile {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 0.75rem;
    padding: 0.75rem 1.1rem; min-width: 150px; flex: 1;
}
.tile-label { color: var(--text-secondary); font-size: 0.78rem; margin-bottom: 0.3rem; }
.tile-value { font-size: 1.3rem; font-weight: 600; }
.table-wrap { overflow-x: auto; background: var(--surface-1); border: 1px solid var(--border); border-radius: 0.75rem; margin-bottom: 1rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid var(--gridline); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.row-error td.num { color: var(--critical); }
"""


def html_escape(text) -> str:
    if not isinstance(text, str):
        text = str(text)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#x27;"))


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
        
    # Load external prompts if present
    prompts_path = workspace_dir / "prompts.yml"
    prompts = {}
    try:
        if prompts_path.exists():
            with open(prompts_path, "r", encoding="utf-8") as f:
                prompts = yaml.safe_load(f) or {}
    except Exception:
        pass
        
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
        "Authorization": f"Bearer {settings.llm_api_key}",
        **OMNIROUTE_NO_CACHE_HEADER,
    }
    http_client = httpx.Client(timeout=1800.0)
    
    sections = []

    for res_file in result_files:
        with open(res_file, "r") as f:
            res_payload = json.load(f)

        config_name = res_payload["configuration_name"]
        result_model = res_payload.get("triage_model", "unknown")
        results = res_payload["results"]

        table_rows = []
        total_acc, total_con, total_act, scored_count = 0, 0, 0, 0

        for r in results:
            if r["triage_level"] != 2 or not r.get("summary"):
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
            
            # Reset per-email so the error path below can't report a *previous* email's response
            # (and can report the raw body when the failure is in the envelope, not the content).
            resp = None
            content = None

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
                    judge_system = prompts.get("auto_rater_summarizer_judge", {}).get("system")
                    if not judge_system:
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
                        "temperature": 0.0,
                        "include_reasoning": False,
                        # Must match what the rest of the suite sends: the proxy streams Server-Sent
                        # Events unless streaming is explicitly declined, and resp.json() cannot
                        # parse an SSE body (it fails with "Expecting value: line 1 column 1").
                        "stream": False,
                        "max_tokens": MAX_TOKENS_SUMMARY_JUDGE
                    }
                    logger.info("Requesting quality score from judge for email: %s", subject)
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                    resp.raise_for_status()
                    if "text/event-stream" in resp.headers.get("content-type", ""):
                        raise RuntimeError(
                            "judge endpoint returned a streaming (SSE) response despite stream=false"
                        )

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

                table_rows.append(f"""<tr>
                    <td>{html_escape(subject)}</td>
                    <td class="num">{acc}/10</td>
                    <td class="num">{con}/10</td>
                    <td class="num">{act}/10</td>
                    <td>{html_escape(rat)}</td>
                </tr>""")
            except Exception as e:
                logger.error("Failed to judge summary quality for email '%s': %s", subject, e)
                if content is not None:
                    logger.error("Raw unparsed judge content was: \n%s", content)
                elif resp is not None:
                    logger.error(
                        "Judge HTTP %s (content-type %r), raw body was: \n%s",
                        resp.status_code, resp.headers.get("content-type"), resp.text[:2000],
                    )
                table_rows.append(f"""<tr class="row-error">
                    <td>{html_escape(subject)}</td>
                    <td class="num">Error</td><td class="num">Error</td><td class="num">Error</td>
                    <td>Audit call failed: {html_escape(str(e))}</td>
                </tr>""")

        if scored_count > 0:
            avg_acc = total_acc / scored_count
            avg_con = total_con / scored_count
            avg_act = total_act / scored_count
            tiles = f"""
            <div class="tiles">
                <div class="tile"><div class="tile-label">Avg accuracy</div><div class="tile-value">{avg_acc:.2f}/10</div></div>
                <div class="tile"><div class="tile-label">Avg conciseness</div><div class="tile-value">{avg_con:.2f}/10</div></div>
                <div class="tile"><div class="tile-label">Avg actionability</div><div class="tile-value">{avg_act:.2f}/10</div></div>
                <div class="tile"><div class="tile-label">Summaries scored</div><div class="tile-value">{scored_count}</div></div>
            </div>
            """
            body = f"""
            {tiles}
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Email subject</th><th>Accuracy</th><th>Conciseness</th><th>Actionability</th><th>Judge rationale</th></tr></thead>
                    <tbody>{"".join(table_rows)}</tbody>
                </table>
            </div>
            """
        else:
            body = '<p class="muted">No escalated summaries generated to evaluate for this configuration.</p>'

        sections.append(f"""
        <h2>📌 {html_escape(config_name)}</h2>
        {body}
        """)

    report = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auto Rater: Executive Summarization Quality Report</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<div class="container">
    <h1>📝 Auto Rater: High-Fidelity Executive Summarization Quality Report</h1>
    <p class="subtitle">Evaluated with LLM-as-a-Judge using model: <code>{html_escape(judge_model)}</code>.</p>
    {"".join(sections)}
</div>
</body>
</html>
"""

    output_report_path = data_dir / "auto_rater_summarizer_report.html"
    with open(output_report_path, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info("Successfully compiled Summarization Quality Report to %s", output_report_path)

if __name__ == "__main__":
    main()
