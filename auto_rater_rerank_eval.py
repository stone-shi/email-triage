"""
Evaluates the Level 0.5 rerank noise filter (EmailTriageEngine.run_rerank_router)
in isolation: precision/recall/F1 against an independent ground truth, swept
across a range of noise-score thresholds, not just whatever tei_noise_threshold
happens to be configured right now.

Unlike auto_rater_runner.py's Level 0 audit (which only judges what the static
filter *flagged*, i.e. precision only), this script judges every email that
would actually reach the Level 0.5 stage in production -- VIP and static-filter
hits are excluded up front since the rerank filter never sees them there
either -- so it can also measure recall (how much real noise the filter would
have missed at a given threshold).

Ground truth defaults to an LLM judge (cached by message_id, cheap to rerun),
but --golden swaps that out for a human-labeled results file (e.g.
auto_rater_results_platinum_human.json) when one exists -- no judge
calls are made at all in that mode, and only messages present in that file are
scored.

Usage:
    ./venv/bin/python3 auto_rater_rerank_eval.py
    ./venv/bin/python3 auto_rater_rerank_eval.py --config tei_classifier_pair
    ./venv/bin/python3 auto_rater_rerank_eval.py --thresholds 0.9,0.95,0.99
    ./venv/bin/python3 auto_rater_rerank_eval.py --force   # ignore judge cache
    ./venv/bin/python3 auto_rater_rerank_eval.py --golden  # use the platinum human set instead of a judge
"""

import json
import logging
import argparse
import yaml
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import settings
from db import EmailDB
from triage import EmailTriageEngine, RERANK_NOISE_ANCHOR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_rerank_eval: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_rerank_eval")

DEFAULT_THRESHOLDS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999, 0.9995, 0.9999]

DEFAULT_AUDIT_SYSTEM = (
    "You are an expert email auditor. Review the email metadata to determine if it is truly low priority noise "
    "(e.g., automated notifications, transactional marketing, newsletters, spam) or if it was a false positive "
    "that actually contains high priority business communication or a critical personal update.\n"
    "You MUST return a valid JSON object containing exactly three fields: "
    "'is_actually_low_priority' (boolean), 'reason' (string), and 'confidence_score' (float from 0.0 to 1.0)."
)


def extract_json(text: str) -> str:
    import re
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    text = re.sub(r'("tag":\s*)(?!(?:true|false|null)\b)([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*[,}])', r'\1"\2"', text)
    text = text.replace("\\'", "'")
    return text


def judge_is_noise(
    http_client: httpx.Client, base_url: str, headers: Dict[str, str], judge_model: str,
    sender: str, subject: str, snippet: str, audit_system: str,
) -> Tuple[bool, float, str]:
    prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
    payload = {
        "model": judge_model,
        "messages": [
            {"role": "system", "content": audit_system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "include_reasoning": False,
    }
    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
    resp.raise_for_status()
    audit_dict = json.loads(extract_json(resp.json()["choices"][0]["message"]["content"]))
    return (
        bool(audit_dict.get("is_actually_low_priority", True)),
        float(audit_dict.get("confidence_score", 1.0)),
        str(audit_dict.get("reason", "")),
    )


def confusion_at_threshold(scored: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    tp = fp = fn = tn = 0
    for item in scored:
        predicted_noise = item["noise_score"] >= threshold
        actual_noise = item["judge_is_noise"]
        if predicted_noise and actual_noise:
            tp += 1
        elif predicted_noise and not actual_noise:
            fp += 1
        elif not predicted_noise and actual_noise:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall and (precision + recall) > 0 else None
    accuracy = (tp + tn) / len(scored) if scored else None
    return {
        "threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "flagged": tp + fp, "precision": precision, "recall": recall, "f1": f1, "accuracy": accuracy,
    }


def _fmt(v: Optional[float]) -> str:
    return f"{v:.3f}" if v is not None else "n/a"


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def html_escape(text: Any) -> str:
    if not isinstance(text, str):
        text = str(text)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#x27;"))


def _examples_html(scored: List[Dict[str, Any]], predicate, label: str, limit: int = 5) -> str:
    matches = sorted((s for s in scored if predicate(s)), key=lambda s: s["noise_score"], reverse=True)
    if not matches:
        return ""
    rows = "".join(
        f"""<tr>
            <td class="num">{s['noise_score']:.4f}</td>
            <td>{html_escape(s['subject'])}</td>
            <td class="muted">{html_escape(s['sender'])}</td>
            <td class="muted">{html_escape(s['judge_reason'])}</td>
        </tr>"""
        for s in matches[:limit]
    )
    return f"""
    <h2>{label} <span class="muted small">(top {min(limit, len(matches))} of {len(matches)})</span></h2>
    <div class="table-wrap">
        <table>
            <thead><tr><th>Score</th><th>Subject</th><th>Sender</th><th>Reason</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """


def build_html_report(scored: List[Dict[str, Any]], rows: List[Dict[str, Any]], current_threshold: float, ground_truth_label: str) -> str:
    total = len(scored)
    if total == 0:
        return "<html><body style='background:#0d0d0d;color:#fff;font-family:sans-serif;padding:2rem;'>No eligible emails found.</body></html>"

    actual_noise_count = sum(1 for s in scored if s["judge_is_noise"])
    noise_rate = actual_noise_count / total

    best_row = max((r for r in rows if r["f1"] is not None), key=lambda r: r["f1"], default=None)
    current_row = next((r for r in rows if abs(r["threshold"] - current_threshold) < 1e-6), None)
    if current_row is None:
        current_row = confusion_at_threshold(scored, current_threshold)

    stat_tiles = f"""
    <div class="tiles">
        <div class="tile"><div class="tile-label">Emails evaluated</div><div class="tile-value">{total}</div></div>
        <div class="tile"><div class="tile-label">Actually noise ({html_escape(ground_truth_label)})</div><div class="tile-value">{_fmt_pct(noise_rate)}</div></div>
        <div class="tile"><div class="tile-label">Current threshold ({current_threshold})</div><div class="tile-value">F1 {_fmt(current_row['f1'])}</div></div>
        {f'<div class="tile tile-good"><div class="tile-label">Best F1 at threshold {best_row["threshold"]}</div><div class="tile-value">F1 {_fmt(best_row["f1"])}</div></div>' if best_row else ''}
    </div>
    """

    # -- SVG line chart: precision / recall / F1 across the threshold sweep (ordinal x-axis) --
    W, H = 900, 380
    pad_l, pad_r, pad_t, pad_b = 48, 24, 24, 44
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    n = len(rows)
    step_x = plot_w / max(n - 1, 1)

    def x_at(i: int) -> float:
        return pad_l + i * step_x

    def y_at(v: Optional[float]) -> float:
        v = v if v is not None else 0.0
        return pad_t + plot_h * (1 - v)

    series = [
        ("precision", "var(--series-1)"),
        ("recall", "var(--series-2)"),
        ("f1", "var(--series-3)"),
    ]
    paths = []
    dots = []
    for key, color in series:
        pts = " ".join(f"{x_at(i):.1f},{y_at(r[key]):.1f}" for i, r in enumerate(rows))
        paths.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
                      f'stroke-linejoin="round" stroke-linecap="round" />')
        for i, r in enumerate(rows):
            if r[key] is not None:
                dots.append(f'<circle class="pt" data-idx="{i}" data-series="{key}" '
                             f'cx="{x_at(i):.1f}" cy="{y_at(r[key]):.1f}" r="4" fill="{color}" '
                             f'stroke="var(--surface-1)" stroke-width="2" />')

    gridlines = "".join(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h * frac:.1f}" x2="{pad_l + plot_w}" y2="{pad_t + plot_h * frac:.1f}" '
        f'stroke="var(--gridline)" stroke-width="1" />'
        for frac in (0, 0.25, 0.5, 0.75, 1.0)
    )
    y_labels = "".join(
        f'<text x="{pad_l - 8}" y="{pad_t + plot_h * (1 - frac) + 4:.1f}" text-anchor="end" class="axis-label">{frac:.2f}</text>'
        for frac in (0, 0.25, 0.5, 0.75, 1.0)
    )
    # thin out x tick labels if there are too many to avoid collision
    label_stride = max(1, n // 10)
    x_labels = "".join(
        f'<text x="{x_at(i):.1f}" y="{pad_t + plot_h + 20}" text-anchor="middle" class="axis-label">{r["threshold"]}</text>'
        for i, r in enumerate(rows) if i % label_stride == 0
    )
    current_idx = next((i for i, r in enumerate(rows) if abs(r["threshold"] - current_threshold) < 1e-6), None)
    current_marker = (
        f'<line x1="{x_at(current_idx):.1f}" y1="{pad_t}" x2="{x_at(current_idx):.1f}" y2="{pad_t + plot_h}" '
        f'stroke="var(--text-secondary)" stroke-width="1" stroke-dasharray="3,3" />'
        if current_idx is not None else ""
    )

    chart_data_json = json.dumps([
        {"threshold": r["threshold"], "precision": r["precision"], "recall": r["recall"], "f1": r["f1"]}
        for r in rows
    ])

    chart_html = f"""
    <h2>Precision / recall / F1 across thresholds</h2>
    <div class="legend">
        <span class="legend-item"><span class="swatch" style="background:var(--series-1)"></span>Precision</span>
        <span class="legend-item"><span class="swatch" style="background:var(--series-2)"></span>Recall</span>
        <span class="legend-item"><span class="swatch" style="background:var(--series-3)"></span>F1</span>
    </div>
    <div class="chart-wrap">
        <svg id="rerank-chart" viewBox="0 0 {W} {H}" role="img" aria-label="Precision, recall, and F1 across noise-score thresholds">
            {gridlines}
            <line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" y2="{pad_t + plot_h}" stroke="var(--axis)" stroke-width="1" />
            {current_marker}
            {"".join(paths)}
            {"".join(dots)}
            {y_labels}
            {x_labels}
            <rect id="hover-rect" x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="transparent" />
            <line id="crosshair" x1="0" y1="{pad_t}" x2="0" y2="{pad_t + plot_h}" stroke="var(--text-secondary)" stroke-width="1" opacity="0" />
        </svg>
        <div id="tooltip" class="tooltip" hidden></div>
    </div>
    <script>
        (function() {{
            const data = {chart_data_json};
            const padL = {pad_l}, plotW = {plot_w}, n = {n};
            const stepX = plotW / Math.max(n - 1, 1);
            const svg = document.getElementById("rerank-chart");
            const hoverRect = document.getElementById("hover-rect");
            const crosshair = document.getElementById("crosshair");
            const tooltip = document.getElementById("tooltip");
            function fmt(v) {{ return v === null || v === undefined ? "n/a" : v.toFixed(3); }}
            hoverRect.addEventListener("mousemove", function(evt) {{
                const rect = svg.getBoundingClientRect();
                const scaleX = {W} / rect.width;
                const xInSvg = (evt.clientX - rect.left) * scaleX;
                let idx = Math.round((xInSvg - padL) / stepX);
                idx = Math.max(0, Math.min(n - 1, idx));
                const d = data[idx];
                const cx = padL + idx * stepX;
                crosshair.setAttribute("x1", cx);
                crosshair.setAttribute("x2", cx);
                crosshair.setAttribute("opacity", "1");
                tooltip.hidden = false;
                tooltip.innerHTML = "<strong>threshold " + d.threshold + "</strong><br>" +
                    "precision: " + fmt(d.precision) + "<br>recall: " + fmt(d.recall) + "<br>F1: " + fmt(d.f1);
                tooltip.style.left = (evt.clientX - rect.left + 12) + "px";
                tooltip.style.top = (evt.clientY - rect.top - 12) + "px";
            }});
            hoverRect.addEventListener("mouseleave", function() {{
                crosshair.setAttribute("opacity", "0");
                tooltip.hidden = true;
            }});
        }})();
    </script>
    """

    table_rows = []
    for row in rows:
        is_current = abs(row["threshold"] - current_threshold) < 1e-6
        is_best = best_row is not None and abs(row["threshold"] - best_row["threshold"]) < 1e-6
        badges = ""
        if is_current:
            badges += '<span class="badge badge-current">current</span>'
        if is_best:
            badges += '<span class="badge badge-best">best F1</span>'
        table_rows.append(f"""<tr class="{'row-current' if is_current else ''}">
            <td class="num">{row['threshold']}{badges}</td>
            <td class="num">{row['flagged']}</td>
            <td class="num">{row['tp']}</td>
            <td class="num">{row['fp']}</td>
            <td class="num">{row['fn']}</td>
            <td class="num">{row['tn']}</td>
            <td class="num">{_fmt(row['precision'])}</td>
            <td class="num">{_fmt(row['recall'])}</td>
            <td class="num">{_fmt(row['f1'])}</td>
            <td class="num">{_fmt(row['accuracy'])}</td>
        </tr>""")

    table_html = f"""
    <h2>Full threshold sweep</h2>
    <div class="table-wrap">
        <table>
            <thead><tr>
                <th>Threshold</th><th>Flagged</th><th>TP</th><th>FP</th><th>FN</th><th>TN</th>
                <th>Precision</th><th>Recall</th><th>F1</th><th>Accuracy</th>
            </tr></thead>
            <tbody>{"".join(table_rows)}</tbody>
        </table>
    </div>
    """

    fp_html = _examples_html(scored, lambda s: s["noise_score"] >= current_threshold and not s["judge_is_noise"], "❌ False positives at current threshold")
    fn_html = _examples_html(scored, lambda s: s["noise_score"] < current_threshold and s["judge_is_noise"], "⚠️ False negatives at current threshold")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rerank Noise Filter: Precision/Recall Evaluation</title>
<style>
    :root {{
        color-scheme: light;
        --surface-1: #fcfcfb; --page: #f9f9f7;
        --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
        --gridline: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
        --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
        --good: #0ca30c; --critical: #d03b3b;
    }}
    @media (prefers-color-scheme: dark) {{
        :root {{
            color-scheme: dark;
            --surface-1: #1a1a19; --page: #0d0d0d;
            --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
            --gridline: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
            --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
            --good: #0ca30c; --critical: #e66767;
        }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
        background: var(--page); color: var(--text-primary);
        margin: 0; padding: 2rem; line-height: 1.5;
    }}
    .container {{ max-width: 1080px; margin: 0 auto; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
    h2 {{ font-size: 1.1rem; margin-top: 2.5rem; margin-bottom: 0.75rem; }}
    p.subtitle {{ color: var(--text-secondary); margin-top: 0; }}
    .small {{ font-size: 0.8rem; font-weight: 400; }}
    .muted {{ color: var(--text-secondary); }}
    .tiles {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-top: 1.5rem; }}
    .tile {{
        background: var(--surface-1); border: 1px solid var(--border); border-radius: 0.75rem;
        padding: 1rem 1.25rem; min-width: 180px; flex: 1;
    }}
    .tile-good {{ border-color: var(--good); }}
    .tile-label {{ color: var(--text-secondary); font-size: 0.8rem; margin-bottom: 0.35rem; }}
    .tile-value {{ font-size: 1.5rem; font-weight: 600; }}
    .legend {{ display: flex; gap: 1.25rem; margin-bottom: 0.5rem; }}
    .legend-item {{ display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem; color: var(--text-secondary); }}
    .swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
    .chart-wrap {{ position: relative; background: var(--surface-1); border: 1px solid var(--border); border-radius: 0.75rem; padding: 1rem; }}
    .axis-label {{ fill: var(--muted); font-size: 11px; }}
    .tooltip {{
        position: absolute; background: var(--text-primary); color: var(--page);
        padding: 0.5rem 0.65rem; border-radius: 0.4rem; font-size: 0.8rem; pointer-events: none;
        white-space: nowrap; z-index: 10;
    }}
    .table-wrap {{ overflow-x: auto; background: var(--surface-1); border: 1px solid var(--border); border-radius: 0.75rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th, td {{ padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid var(--gridline); }}
    th {{ color: var(--text-secondary); font-weight: 600; white-space: nowrap; }}
    td.num {{ font-variant-numeric: tabular-nums; }}
    tr.row-current {{ background: color-mix(in srgb, var(--series-1) 8%, transparent); }}
    .badge {{ display: inline-block; margin-left: 0.5rem; padding: 0.1rem 0.4rem; border-radius: 0.3rem; font-size: 0.7rem; font-weight: 600; }}
    .badge-current {{ background: color-mix(in srgb, var(--series-1) 20%, transparent); color: var(--series-1); }}
    .badge-best {{ background: color-mix(in srgb, var(--good) 20%, transparent); color: var(--good); }}
</style>
</head>
<body>
<div class="container">
    <h1>🎯 Rerank Noise Filter: Precision/Recall Evaluation</h1>
    <p class="subtitle">Evaluated {total} emails that would reach the Level 0.5 stage in production (VIP-bypass and static-filter hits excluded).</p>
    {stat_tiles}
    {chart_html}
    {table_html}
    {fp_html}
    {fn_html}
</div>
</body>
</html>
"""


def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    data_dir = workspace_dir / "auto_rater_data"
    emails_path = data_dir / "offline_emails.json"
    cache_path = data_dir / "rerank_eval_judge_cache.json"

    if not emails_path.exists():
        logger.error("No offline dataset found at %s -- run auto_rater_downloader.py first.", emails_path)
        sys.exit(1)

    config_data: Dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

    judge_model = config_data.get("level_0_judge_model") or config_data.get("judge_model", "deepseek/deepseek-v4-pro")

    parser = argparse.ArgumentParser(description="Rerank Noise Filter Precision/Recall Evaluator")
    parser.add_argument("--config", type=str, help="Name of a test_configurations entry to pull tei_url/tei_model/tei_api_key from")
    parser.add_argument("--judge-model", type=str, help="Override the judge model used for ground truth")
    parser.add_argument("--thresholds", type=str, help="Comma-separated list of noise-score thresholds to sweep")
    parser.add_argument("-f", "--force", action="store_true", help="Ignore the judge cache and re-audit every email")
    parser.add_argument(
        "--golden", nargs="?", const="auto_rater_results_platinum_human.json", default=None,
        help="Use a human-labeled golden/platinum results file (triage_level==0 counts as noise) as ground "
             "truth instead of an LLM judge -- no judge calls are made at all. Give a filename relative to "
             "auto_rater_data/ (or an absolute path); bare --golden defaults to "
             "auto_rater_results_platinum_human.json. Only messages present in that file are scored, "
             "so results will be limited to however many emails it labels.",
    )
    args = parser.parse_args()

    if args.judge_model:
        judge_model = args.judge_model
    thresholds = DEFAULT_THRESHOLDS
    if args.thresholds:
        thresholds = sorted(float(t.strip()) for t in args.thresholds.split(","))

    prompts_path = workspace_dir / "prompts.yml"
    prompts = {}
    try:
        if prompts_path.exists():
            with open(prompts_path, "r", encoding="utf-8") as f:
                prompts = yaml.safe_load(f) or {}
    except Exception:
        pass
    audit_system = prompts.get("auto_rater_level_0_audit", {}).get("system") or DEFAULT_AUDIT_SYSTEM

    with open(emails_path, "r", encoding="utf-8") as f:
        emails = json.load(f)

    old_tei_url = settings.triage.tei_url
    old_tei_model = settings.triage.tei_model
    old_tei_api_key = settings.triage.tei_api_key
    if args.config:
        matched = next((c for c in config_data.get("test_configurations", []) if c.get("name") == args.config), None)
        if not matched:
            logger.error("No test_configurations entry found matching name: '%s'", args.config)
            sys.exit(1)
        if "tei_url" in matched:
            settings.triage.tei_url = matched["tei_url"]
        if "tei_model" in matched:
            settings.triage.tei_model = matched["tei_model"]
        if "tei_api_key" in matched:
            settings.triage.tei_api_key = matched["tei_api_key"]
        logger.info("Using reranker endpoint from config '%s': %s (%s)", args.config, settings.triage.tei_url, settings.triage.tei_model)

    golden_lookup: Optional[Dict[str, int]] = None
    ground_truth_label = f"judge model {judge_model}"
    if args.golden:
        golden_path = Path(args.golden)
        if not golden_path.is_absolute():
            golden_path = data_dir / golden_path
        if not golden_path.exists():
            logger.error("Golden/platinum file not found at %s", golden_path)
            sys.exit(1)
        with open(golden_path, "r", encoding="utf-8") as f:
            golden_data = json.load(f)
        golden_lookup = {r["message_id"]: r["triage_level"] for r in golden_data.get("results", [])}
        ground_truth_label = f"golden set {golden_path.name}"
        logger.info("Using golden/platinum ground truth from %s (%d labeled messages) -- no judge calls will be made.",
                    golden_path, len(golden_lookup))

    judge_cache: Dict[str, Dict[str, Any]] = {}
    if golden_lookup is None and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                judge_cache = json.load(f)
            logger.info("Loaded %d cached judge audits from disk.", len(judge_cache))
        except Exception:
            judge_cache = {}

    dummy_db = EmailDB(db_path=workspace_dir / "email_cache.db")
    engine = EmailTriageEngine(dummy_db)
    base_url = settings.llm_base_url.rstrip("/")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.llm_api_key}"}

    scored: List[Dict[str, Any]] = []
    skipped_vip = skipped_static = skipped_unlabeled = errors = 0

    try:
        for idx, email in enumerate(emails, 1):
            sender = email["sender"]
            subject = email["subject"]
            snippet = email["snippet"]
            msg_id = email["message_id"]

            if engine.is_vip_sender(sender):
                skipped_vip += 1
                continue
            is_static_noise, _ = engine.run_level_0_static(sender, subject)
            if is_static_noise:
                skipped_static += 1
                continue

            if golden_lookup is not None and msg_id not in golden_lookup:
                skipped_unlabeled += 1
                continue

            query_text = f"From: {sender} | Subject: {subject} | Snippet: {snippet}"
            try:
                noise_score = engine._rerank(query_text, [RERANK_NOISE_ANCHOR])[0]
            except Exception as e:
                logger.warning("[%d/%d] Rerank call failed for %s: %s", idx, len(emails), msg_id, e)
                errors += 1
                continue

            if golden_lookup is not None:
                judge_noise = golden_lookup[msg_id] == 0
                judge_conf, judge_reason = 1.0, "golden/platinum label"
            else:
                cache_key = f"{judge_model}||{msg_id}"
                cached = judge_cache.get(cache_key)
                if cached is not None and not args.force:
                    judge_noise, judge_conf, judge_reason = cached["is_noise"], cached["confidence"], cached["reason"]
                else:
                    try:
                        judge_noise, judge_conf, judge_reason = judge_is_noise(
                            engine.http_client, base_url, headers, judge_model, sender, subject, snippet, audit_system
                        )
                    except Exception as e:
                        logger.warning("[%d/%d] Judge audit failed for %s: %s", idx, len(emails), msg_id, e)
                        errors += 1
                        continue
                    judge_cache[cache_key] = {"is_noise": judge_noise, "confidence": judge_conf, "reason": judge_reason}

            scored.append({
                "message_id": msg_id, "sender": sender, "subject": subject,
                "noise_score": noise_score, "judge_is_noise": judge_noise,
                "judge_confidence": judge_conf, "judge_reason": judge_reason,
            })

            if idx % 20 == 0:
                logger.info("[%d/%d] scored so far: %d, skipped VIP: %d, skipped static: %d, unlabeled: %d, errors: %d",
                            idx, len(emails), len(scored), skipped_vip, skipped_static, skipped_unlabeled, errors)
                if golden_lookup is None:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(judge_cache, f, indent=2, ensure_ascii=False)
    finally:
        settings.triage.tei_url = old_tei_url
        settings.triage.tei_model = old_tei_model
        settings.triage.tei_api_key = old_tei_api_key
        if golden_lookup is None:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(judge_cache, f, indent=2, ensure_ascii=False)

    if not scored:
        logger.error("No eligible emails were scored (skipped VIP: %d, skipped static: %d, unlabeled: %d, errors: %d). Nothing to report.",
                     skipped_vip, skipped_static, skipped_unlabeled, errors)
        sys.exit(1)

    all_thresholds = sorted(set(thresholds) | {round(settings.triage.tei_noise_threshold, 6)})
    rows = [confusion_at_threshold(scored, t) for t in all_thresholds]
    report = build_html_report(scored, rows, settings.triage.tei_noise_threshold, ground_truth_label)

    report_path = data_dir / "auto_rater_rerank_eval_report.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info("Scored %d eligible emails (skipped %d VIP, %d static-filtered, %d unlabeled, %d errors). Report written to %s",
                len(scored), skipped_vip, skipped_static, skipped_unlabeled, errors, report_path)


if __name__ == "__main__":
    main()
