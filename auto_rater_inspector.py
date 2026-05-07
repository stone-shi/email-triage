import json
import logging
import sys
import argparse
from pathlib import Path
from typing import List, Dict, Any

def print_level_0(results: List[Dict[str, Any]]) -> None:
    border = "-" * 70
    print(f"\n======================================================================")
    print(f"🚫 LEVEL 0 STATIC REGEX NOISE FILTER HITS ({len(results)} emails)")
    print(f"======================================================================")
    
    for idx, r in enumerate(results, 1):
        print(f"\n[{idx}] 📧 Subject: {r['subject']}")
        print(f"    From:    {r['sender']}")
        print(f"    Regex Hit Reason: {r['reason']}")
        print(f"    🤖 Judge Correctness Audit: **{r.get('level_0_judge_correctness', 'N/A')}** (Confidence: {r.get('level_0_judge_score', 1.0)})")
        print(f"    🤖 Judge Audit Rationale:   {r.get('level_0_judge_reason', 'None')}")
        print(border)

def print_level_1(results: List[Dict[str, Any]]) -> None:
    border = "-" * 70
    print(f"\n======================================================================")
    print(f"📉 LEVEL 1 LOW-COST LOW PRIORITY FILTER HITS ({len(results)} emails)")
    print(f"======================================================================")
    
    for idx, r in enumerate(results, 1):
        print(f"\n[{idx}] 📧 Subject: {r['subject']}")
        print(f"    From:    {r['sender']}")
        print(f"    Model Triage Reason: {r['reason']}")
        print(f"    Confidence Score:     {r['score']}")
        print(border)

def print_level_2(results: List[Dict[str, Any]]) -> None:
    border = "-" * 70
    print(f"\n======================================================================")
    print(f"🔥 LEVEL 2 PREMIUM CRITICAL ALERT ESCALATIONS ({len(results)} emails)")
    print(f"======================================================================")
    
    for idx, r in enumerate(results, 1):
        print(f"\n[{idx}] 📧 Subject: {r['subject']}")
        print(f"    From:    {r['sender']}")
        print(f"    Escalation Reason: {r['reason']}")
        print(f"    ✨ Bulleted Executive Summary:")
        summary = r.get('summary')
        if summary:
            print(f"    {summary.replace('\n', '\n    ')}")
        else:
            print(f"    (No summary generated)")
        print(border)

def inspect_file(file_path: Path, level_filter: str = None, wrong_only: bool = False) -> None:
    if not file_path.exists():
        print(f"Error: File not found at {file_path}")
        return
        
    with open(file_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
        
    config_name = payload.get("configuration_name", "Unknown Configuration")
    results = payload.get("results", [])
    
    print("\n" + "#" * 80)
    print(f"🔍 AUTO RATER HUMAN INSPECTION INTERFACE FOR CONFIG: '{config_name}'")
    print(f"   Total Batch Processed: {payload.get('total_emails_processed', 0)} emails")
    print(f"   Total Process Latency: {payload.get('total_processing_all_emails_duration_sec', 0.0):.2f}s")
    print("#" * 80)
    
    l0_list = [r for r in results if r.get("triage_level") == "Level 0"]
    if wrong_only:
        l0_list = [r for r in l0_list if r.get("level_0_judge_correctness") == "False Positive"]
        
    l1_list = [r for r in results if r.get("triage_level") == "Level 1" or r.get("triage_level") == "Level 1 (Escalated)"]
    l2_list = [r for r in results if r.get("triage_level") == "Level 2"]
    
    if level_filter is None or level_filter == "0":
        print_level_0(l0_list)
    if level_filter is None or level_filter == "1":
        print_level_1(l1_list)
    if level_filter is None or level_filter == "2":
        print_level_2(l2_list)

def main() -> None:
    parser = argparse.ArgumentParser(description="Auto Rater Human Inspection Interface Utility")
    parser.add_argument("--file", type=str, help="Specific target auto_rater_results_*.json file name inside data directory to inspect")
    parser.add_argument("--level", type=str, choices=["0", "1", "2"], help="Filter display to show only a single triage level tier group (0, 1, or 2)")
    parser.add_argument("--wrong-only", action="store_true", help="Display only Level 0 emails where the judge determined the static filter was a False Positive")
    args = parser.parse_args()
    
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = workspace_dir / "auto_rater_data"
    
    if args.file:
        target_path = data_dir / args.file
        inspect_file(target_path, level_filter=args.level, wrong_only=args.wrong_only)
    else:
        # List available results JSON files for easy user selection menu
        result_files = list(data_dir.glob("auto_rater_results_*.json"))
        if not result_files:
            print(f"No benchmark results files found inside data directory: {data_dir}")
            sys.exit(1)
            
        print("\n📋 Available Auto Rater Benchmark Result Datasets for Inspection:")
        for idx, f_path in enumerate(result_files, 1):
            print(f"  [{idx}] {f_path.name}")
            
        print(f"\nPlease rerun with `--file <filename>` parameter choice from list above.")
        print(f"Example: python3 auto_rater_inspector.py --file {result_files[0].name}\n")

if __name__ == "__main__":
    main()
