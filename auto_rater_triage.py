import json
import logging
import argparse
import yaml
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_triage: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_triage")

# Shared CSS shell -- same look as auto_rater_rerank_eval.py / auto_rater_mmbert_eval.py's
# reports, so all three read as one family of report.
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
h3 { font-size: 1rem; margin-top: 1.5rem; margin-bottom: 0.5rem; color: var(--text-secondary); }
p.subtitle { color: var(--text-secondary); margin-top: 0; }
.muted { color: var(--text-secondary); }
.table-wrap { overflow-x: auto; background: var(--surface-1); border: 1px solid var(--border); border-radius: 0.75rem; margin-bottom: 1rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid var(--gridline); }
th { color: var(--text-secondary); font-weight: 600; white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; }
tr.row-best { background: color-mix(in srgb, var(--good) 8%, transparent); }
"""


def html_escape(text: Any) -> str:
    if not isinstance(text, str):
        text = str(text)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#x27;"))

def get_summary_stats(data: Dict[str, Any], judge_cache: Dict[str, Any], judge_model: str, important_msg_ids: List[str] = None):
    total_acc, total_con, total_act, count = 0, 0, 0, 0
    triage_model = data.get("triage_model", "unknown")
    
    for r in data["results"]:
        msg_id = r["message_id"]
        if important_msg_ids is not None and msg_id not in important_msg_ids:
            continue
        
        if r.get("summary"):
            summary_text = r["summary"]
            # Cache key: triage_model||judge_model||msg_id||summary
            cache_key = f"{triage_model}||{judge_model}||{msg_id}||{summary_text}"
            if cache_key in judge_cache:
                scores = judge_cache[cache_key].get("scores", {})
                total_acc += scores.get("accuracy", 0)
                total_con += scores.get("conciseness", 0)
                total_act += scores.get("actionability", 0)
                count += 1
                
    if count == 0:
        return None
    return {
        "accuracy": total_acc / count,
        "conciseness": total_con / count,
        "actionability": total_act / count,
        "count": count
    }

def _alignment_table(configs_data: Dict[str, Any], ground_truth: Dict[str, bool], exclude_name: str,
                      tags: Optional[Dict[str, str]] = None) -> str:
    """Shared confusion-matrix/precision/recall/F1 table builder for both the
    baseline and human-platinum alignment sections -- same math, different
    ground-truth source."""
    rows = []
    for name, data in configs_data.items():
        if name == exclude_name:
            continue

        tp = fp = fn = tn = 0
        tag_matches = tag_total = 0
        for r in data["results"]:
            msg_id = r["message_id"]
            if tags is not None and msg_id in tags and tags[msg_id] != "un-tagged":
                tag_total += 1
                if r.get("tag") == tags[msg_id]:
                    tag_matches += 1

            if r["triage_level"] == 0:
                continue
            if msg_id not in ground_truth:
                continue

            actual_important = ground_truth[msg_id]
            pred_important = (r["triage_level"] == 2)
            if actual_important and pred_important:
                tp += 1
            elif not actual_important and pred_important:
                fp += 1
            elif actual_important and not pred_important:
                fn += 1
            else:
                tn += 1

        scored = tp + fp + fn + tn
        # Everything the config saw, including the level-0 rows and the ones missing
        # from ground truth that the confusion matrix skips -- this is the count that
        # should match the dataset size (e.g. 100 or 20).
        total_samples = len(data["results"])
        accuracy = (tp + tn) / scored if scored else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        tag_acc = (tag_matches / tag_total * 100) if tag_total else None
        rows.append((name, accuracy, precision, recall, f1, tp, fp, fn, tn, tag_acc, scored, total_samples))

    if not rows:
        return ""

    rows.sort(key=lambda r: r[4], reverse=True)

    show_tag_col = tags is not None
    header_extra = "<th>Tag match</th>" if show_tag_col else ""
    body_rows = []
    for idx, (name, accuracy, precision, recall, f1, tp, fp, fn, tn, tag_acc, scored, total_samples) in enumerate(rows):
        is_best = idx == 0 and f1 > 0
        tag_cell = f"<td class=\"num\">{tag_acc:.1f}%</td>" if show_tag_col and tag_acc is not None else ("<td class=\"num muted\">n/a</td>" if show_tag_col else "")
        body_rows.append(f"""<tr class="{'row-best' if is_best else ''}">
            <td>{html_escape(name)}</td>
            <td class="num">{accuracy*100:.1f}%</td>
            <td class="num">{precision*100:.1f}%</td>
            <td class="num">{recall*100:.1f}%</td>
            <td class="num">{f1:.3f}</td>
            <td class="num">{tp}</td><td class="num">{fp}</td><td class="num">{fn}</td><td class="num">{tn}</td>
            <td class="num">{scored}</td>
            <td class="num">{total_samples}</td>
            {tag_cell}
        </tr>""")

    return f"""
    <div class="table-wrap">
        <table>
            <thead><tr>
                <th>Configuration</th><th>Accuracy</th><th>Precision</th><th>Recall</th><th>F1</th>
                <th>TP</th><th>FP</th><th>FN</th><th>TN</th><th>Scored</th><th>Total samples</th>{header_extra}
            </tr></thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </div>
    <p class="muted">Sorted by F1, highest first. <em>Scored</em> = TP+FP+FN+TN, i.e. the rows the
    confusion matrix actually covers (level-0 predictions and messages absent from the ground truth are
    excluded); <em>Total samples</em> = every message in the config's result file.</p>"""


def _summary_quality_table(configs_data: Dict[str, Any], judge_cache: Dict[str, Any], judge_model: str,
                            important_msg_ids: Optional[List[str]] = None) -> str:
    scored = []
    unscored = []
    for name, data in configs_data.items():
        stats = get_summary_stats(data, judge_cache, judge_model, important_msg_ids=important_msg_ids)
        if stats:
            # Overall = unweighted mean of the three rubric dimensions, same 1-10 scale.
            overall = (stats["accuracy"] + stats["conciseness"] + stats["actionability"]) / 3
            scored.append((name, stats, overall))
        else:
            unscored.append(name)

    scored.sort(key=lambda r: r[2], reverse=True)

    rows = []
    for idx, (name, stats, overall) in enumerate(scored):
        rows.append(f"""<tr class="{'row-best' if idx == 0 else ''}">
            <td>{html_escape(name)}</td>
            <td class="num">{overall:.2f}/10</td>
            <td class="num">{stats['accuracy']:.2f}/10</td>
            <td class="num">{stats['conciseness']:.2f}/10</td>
            <td class="num">{stats['actionability']:.2f}/10</td>
            <td class="num">{stats['count']}</td>
        </tr>""")
    for name in unscored:
        rows.append(f"""<tr><td>{html_escape(name)}</td><td class="num">N/A</td><td class="num">N/A</td><td class="num">N/A</td><td class="num">N/A</td><td class="num">0</td></tr>""")
    return f"""
    <div class="table-wrap">
        <table>
            <thead><tr><th>Configuration</th><th>Overall score</th><th>Avg accuracy</th><th>Avg conciseness</th><th>Avg actionability</th><th>Sample count</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>
    <p class="muted">Sorted by overall score (mean of accuracy, conciseness and actionability), highest first.</p>"""


def analyze_results(files: List[Path], baseline_name: str, judge_cache: Dict[str, Any] = None, judge_model: str = None) -> str:
    sections = []

    configs_data = {}
    for f_path in files:
        with open(f_path.resolve(), "r", encoding="utf-8") as f:
            data = json.load(f)
        configs_data[data["configuration_name"]] = data

    # Operational performance
    perf_rows = []
    for name, data in configs_data.items():
        total_time = data["total_processing_all_emails_duration_sec"]
        total_emails = data["total_emails_processed"]
        avg_time = total_time / total_emails if total_emails > 0 else 0
        l1_prompt_tokens = sum(r["level_1_prompt_tokens"] for r in data["results"])
        l1_comp_tokens = sum(r["level_1_completion_tokens"] for r in data["results"])
        perf_rows.append(f"""<tr>
            <td>{html_escape(name)}</td>
            <td class="num">{total_time:.2f}s</td>
            <td class="num">{total_emails}</td>
            <td class="num">{avg_time:.3f}s</td>
            <td class="num">{l1_prompt_tokens}</td>
            <td class="num">{l1_comp_tokens}</td>
        </tr>""")
    sections.append(f"""
    <h2>⚙️ Operational performance</h2>
    <div class="table-wrap">
        <table>
            <thead><tr><th>Configuration</th><th>Total time</th><th>Total emails</th><th>Avg sec/email</th>
                <th>L1 prompt tokens</th><th>L1 completion tokens</th></tr></thead>
            <tbody>{"".join(perf_rows)}</tbody>
        </table>
    </div>
    """)

    # Triage decisions breakdown
    decision_rows = []
    for name, data in configs_data.items():
        total = len(data["results"])
        if total == 0:
            decision_rows.append(f"""<tr>
                <td>{html_escape(name)}</td>
                <td class="num">0</td>
                <td class="muted" colspan="4">No cached results yet -- run auto_rater_runner.py --run {html_escape(name)}</td>
            </tr>""")
            continue
        l0 = sum(1 for r in data["results"] if r["triage_level"] == 0)
        l1 = sum(1 for r in data["results"] if r["triage_level"] == 1)
        l2 = sum(1 for r in data["results"] if r["triage_level"] == 2)
        tags_dist: Dict[str, int] = {}
        for r in data["results"]:
            t = r.get("tag", "un-tagged")
            tags_dist[t] = tags_dist.get(t, 0) + 1
        tag_dist_str = ", ".join(f"{k}: {v}" for k, v in sorted(tags_dist.items()))
        decision_rows.append(f"""<tr>
            <td>{html_escape(name)}</td>
            <td class="num">{total}</td>
            <td class="num">{l0} ({l0/total*100:.1f}%)</td>
            <td class="num">{l1} ({l1/total*100:.1f}%)</td>
            <td class="num">{l2} ({l2/total*100:.1f}%)</td>
            <td class="muted">{html_escape(tag_dist_str)}</td>
        </tr>""")
    sections.append(f"""
    <h2>🎯 Triage decisions breakdown</h2>
    <div class="table-wrap">
        <table>
            <thead><tr><th>Configuration</th><th>Total</th><th>Level 0 (noise)</th><th>Level 1</th>
                <th>Level 2 (important)</th><th>Tag distribution</th></tr></thead>
            <tbody>{"".join(decision_rows)}</tbody>
        </table>
    </div>
    """)

    # Benchmark alignment vs the configured baseline
    if baseline_name in configs_data and len(configs_data) > 1:
        baseline_results = {r["message_id"]: (r["triage_level"] == 2) for r in configs_data[baseline_name]["results"] if r["triage_level"] != 0}
        table = _alignment_table(configs_data, baseline_results, baseline_name)
        if table:
            sections.append(f"""
            <h2>📉 Benchmark alignment (relative to <code>{html_escape(baseline_name)}</code>)</h2>
            {table}
            """)

    # Human platinum alignment
    platinum_name = "platinum_human"
    if platinum_name in configs_data and len(configs_data) > 1:
        plat_results = {r["message_id"]: (r["triage_level"] == 2) for r in configs_data[platinum_name]["results"] if r["triage_level"] != 0}
        plat_tags = {r["message_id"]: r.get("tag", "un-tagged") for r in configs_data[platinum_name]["results"]}
        table = _alignment_table(configs_data, plat_results, platinum_name, tags=plat_tags)
        if table:
            sections.append(f"""
            <h2>💎 Human platinum alignment (gold standard)</h2>
            {table}
            """)

    # Summarization quality
    if judge_cache:
        sections.append(f"""
        <h2>📝 Summarization quality comparison</h2>
        {_summary_quality_table(configs_data, judge_cache, judge_model)}
        """)

        if platinum_name in configs_data:
            human_important_ids = [r["message_id"] for r in configs_data[platinum_name]["results"] if r["triage_level"] == 2]
            if human_important_ids:
                sections.append(f"""
                <h3>Restricted to the human-important gold set</h3>
                {_summary_quality_table(configs_data, judge_cache, judge_model, important_msg_ids=human_important_ids)}
                """)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auto Rater: Email Triage Classification Performance Report</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<div class="container">
    <h1>📊 Auto Rater: Email Triage Classification Performance Report</h1>
    <p class="subtitle">Analyzed {len(files)} test configuration(s).</p>
    {"".join(sections)}
</div>
</body>
</html>
"""

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = (workspace_dir / "auto_rater_data").resolve()
    result_files = list(data_dir.glob("auto_rater_results_*.json"))
    
    if not result_files:
        logger.error("No auto rater JSON results found matching auto_rater_data/auto_rater_results_*.json")
        sys.exit(1)
        
    config_path = workspace_dir / "auto_rater_config.yml"
    baseline_name = "production_deepseek_pair"
    judge_model = "gemini/gemini-3.1-pro-preview" # Default
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as cfg_f:
                config_data = yaml.safe_load(cfg_f) or {}
            baseline_name = config_data.get("baseline_configuration_name", baseline_name)
            judge_model = config_data.get("judge_model", judge_model)
            
            log_level = config_data.get("log_level", "INFO").upper()
            numeric_level = getattr(logging, log_level, logging.INFO)
            logging.getLogger().setLevel(numeric_level)
            logger.setLevel(numeric_level)
        except Exception:
            pass

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Auto Rater Triage Report Compiler Utility")
    parser.add_argument("--compare", type=str, nargs="+", metavar="NAME",
                         help="One or more experimental result configuration names to compare against the baseline "
                              "(space-separated, e.g. --compare gemma4e2b-0 gemma4e4b-0)")
    args = parser.parse_args()

    if args.compare:
        baseline_filename = f"auto_rater_results_{baseline_name}.json"
        compare_filenames = [f"auto_rater_results_{name}.json" for name in args.compare]
        platinum_filename = "auto_rater_results_platinum_human.json"
        wanted_filenames = [baseline_filename, *compare_filenames, platinum_filename]
        result_files = [f for f in result_files if f.name in wanted_filenames]
        logger.info("Targeted comparison active: comparing %s against baseline standard '%s'", args.compare, baseline_name)

    # Load summarizer cache
    cache_path = data_dir / "auto_rater_summarizer_cache.json"
    judge_cache = {}
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                judge_cache = json.load(cf)
            logger.info("Loaded %d existing quality score cache records from disk.", len(judge_cache))
        except Exception as e:
            logger.warning("Could not load summarizer cache: %s", e)

    report = analyze_results(result_files, baseline_name, judge_cache, judge_model)

    output_report_path = workspace_dir / "auto_rater_data" / "auto_rater_triage_report.html"
    try:
        output_report_path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        with open(output_report_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("Successfully compiled Triage Accuracy Report to %s", output_report_path)
    except Exception as e:
        logger.warning("Could not write report to %s (%s).", output_report_path, e)

if __name__ == "__main__":
    main()
