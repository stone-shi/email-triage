import json
import logging
import argparse
import yaml
import sys
from pathlib import Path
from typing import Dict, Any, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_triage: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_triage")

def analyze_results(files: List[Path], baseline_name: str) -> str:
    report_lines = []
    report_lines.append("# 📊 Auto Rater: Email Triage Classification Performance Report")
    report_lines.append(f"Analyzed {len(files)} test configurations.\n")
    
    configs_data = {}
    
    for f_path in files:
        with open(f_path, "r") as f:
            data = json.load(f)
        configs_data[data["configuration_name"]] = data
        
    # Print general table of metrics
    report_lines.append("## ⚙️ Operational Performance Summary Table")
    report_lines.append("| Configuration Name | Total Time (s) | Total Emails | Avg Sec/Email | L1 Prompt Tokens | L1 Completion Tokens |")
    report_lines.append("|---|---|---|---|---|---|")
    
    for name, data in configs_data.items():
        total_time = data["total_processing_all_emails_duration_sec"]
        total_emails = data["total_emails_processed"]
        avg_time = total_time / total_emails if total_emails > 0 else 0
        
        l1_prompt_tokens = sum(r["level_1_prompt_tokens"] for r in data["results"])
        l1_comp_tokens = sum(r["level_1_completion_tokens"] for r in data["results"])
        
        report_lines.append(f"| {name} | {total_time:.2f}s | {total_emails} | {avg_time:.3f}s | {l1_prompt_tokens} | {l1_comp_tokens} |")
        
    report_lines.append("\n## 🎯 Triage Decisions Breakdown")
    
    for name, data in configs_data.items():
        total = len(data["results"])
        l0_filtered = sum(1 for r in data["results"] if r["triage_level"] == "Level 0")
        l1_unimportant = sum(1 for r in data["results"] if r["triage_level"] == "Level 1")
        important = sum(1 for r in data["results"] if r["triage_level"].startswith("Level 2"))
        
        report_lines.append(f"### 🔍 Configuration: `{name}`")
        report_lines.append(f"- **Total Scanned Envelopes**: {total}")
        report_lines.append(f"- **Level 0 Static Noise Intercepted**: {l0_filtered} ({l0_filtered/total*100:.1f}%)")
        report_lines.append(f"- **Level 1 Low-Cost Low Importance Filtered**: {l1_unimportant} ({l1_unimportant/total*100:.1f}%)")
        report_lines.append(f"- **Escalated Critical/Important Emails**: {important} ({important/total*100:.1f}%)")
        report_lines.append("")
        
    # Benchmark Alignment Analysis using the gold baseline configuration if present
    if baseline_name in configs_data and len(configs_data) > 1:
        report_lines.append("## 📉 Benchmark Alignment Analytics (Relative to Baseline)")
        baseline_results = {r["message_id"]: (r["triage_level"].startswith("Level 2")) for r in configs_data[baseline_name]["results"] if r["triage_level"] != "Level 0"}
        
        for name, data in configs_data.items():
            if name == baseline_name:
                continue
            
            tp, fp, fn, tn = 0, 0, 0, 0
            for r in data["results"]:
                if r["triage_level"] == "Level 0":
                    continue
                msg_id = r["message_id"]
                if msg_id not in baseline_results:
                    continue
                    
                actual_important = baseline_results[msg_id]
                pred_important = (r["triage_level"].startswith("Level 2"))
                
                if actual_important and pred_important:
                    tp += 1
                elif not actual_important and pred_important:
                    fp += 1
                elif actual_important and not pred_important:
                    fn += 1
                else:
                    tn += 1
                    
            accuracy = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) > 0 else 0
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            
            report_lines.append(f"### 📊 `{name}` alignment against `{baseline_name}`:")
            report_lines.append(f"- **Relative Classification Accuracy**: {accuracy*100:.1f}%")
            report_lines.append(f"- **Relative Precision**: {precision*100:.1f}%")
            report_lines.append(f"- **Relative Recall**: {recall*100:.1f}%")
            report_lines.append(f"- **Relative F1 Score Balance Metric**: {f1:.3f}")
            report_lines.append(f"- **Confusion Matrix Counts**: [True Important: {tp}, False Important: {fp}, False Noise: {fn}, True Noise: {tn}]")
            report_lines.append("")

    return "\n".join(report_lines)

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = workspace_dir / "auto_rater_data"
    result_files = list(data_dir.glob("auto_rater_results_*.json"))
    
    if not result_files:
        logger.error("No auto rater JSON results found matching auto_rater_data/auto_rater_results_*.json")
        sys.exit(1)
        
    config_path = workspace_dir / "auto_rater_config.yml"
    baseline_name = "production_deepseek_pair"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as cfg_f:
                config_data = yaml.safe_load(cfg_f) or {}
            baseline_name = config_data.get("baseline_configuration_name", baseline_name)
            
            log_level = config_data.get("log_level", "INFO").upper()
            numeric_level = getattr(logging, log_level, logging.INFO)
            logging.getLogger().setLevel(numeric_level)
            logger.setLevel(numeric_level)
        except Exception:
            pass

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Auto Rater Triage Report Compiler Utility")
    parser.add_argument("--compare", type=str, help="Name of a single experimental result configuration to compare against the baseline")
    args = parser.parse_args()
    
    if args.compare:
        baseline_filename = f"auto_rater_results_{baseline_name}.json"
        compare_filename = f"auto_rater_results_{args.compare}.json"
        result_files = [f for f in result_files if f.name == baseline_filename or f.name == compare_filename]
        logger.info("Targeted comparison active: comparing '%s' against baseline standard '%s'", args.compare, baseline_name)

    report = analyze_results(result_files, baseline_name)
    
    output_report_path = data_dir / "auto_rater_triage_report.md"
    with open(output_report_path, "w", encoding="utf-8") as f:
        f.write(report)
        
    logger.info("Successfully compiled Triage Accuracy Report to %s", output_report_path)
    print("\n" + report + "\n")

if __name__ == "__main__":
    main()
