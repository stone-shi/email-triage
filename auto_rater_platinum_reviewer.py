import json
import logging
import sys
from pathlib import Path
from typing import Dict, Any, List

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s", # Keep clean formatting for interactive terminal view
    handlers=[logging.StreamHandler(sys.stdout)]
)

def display_card(idx: int, total: int, email_data: Dict[str, Any], offline_data: Dict[str, Any]) -> None:
    border = "=" * 80
    sub_border = "-" * 80
    
    sender = offline_data.get("sender", email_data.get("sender", "Unknown"))
    subject = offline_data.get("subject", email_data.get("subject", "No Subject"))
    snippet = offline_data.get("snippet", "No Snippet")
    full_body = offline_data.get("full_body", "")
    
    body_preview = full_body[:1500] + ("..." if len(full_body) > 1500 else "")
    
    print(f"\n{border}")
    print(f"⭐ [PLATINUM HUMAN REVIEW]: Email {idx} of {total}")
    print(f"🆔 MESSAGE ID: {email_data.get('message_id')}")
    print(border)
    print(f"📬 SENDER:   {sender}")
    print(f"📝 SUBJECT:  {subject}")
    print(f"📄 SNIPPET:  {snippet}")
    print(sub_border)
    print("📖 ORIGINAL FULL BODY PREVIEW:")
    print(body_preview.strip() if body_preview.strip() else "(No body text found)")
    print(border)
    print(f"🤖 BASELINE DECISION: Triage Level {email_data.get('triage_level')}")
    print(f"💡 BASELINE REASON:   {email_data.get('reason')}")
    summary = email_data.get("summary")
    if summary:
        print(sub_border)
        print("📝 BASELINE SUMMARY:")
        print(summary.strip())
    print(border)

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = (workspace_dir / "auto_rater_data").resolve()
    
    platinum_file = data_dir / "auto_rater_results_baseline_platinum_human.json"
    offline_file = data_dir / "offline_emails.json"
    
    if not platinum_file.exists():
        print(f"\nError: Platinum dataset file not found at {platinum_file.name}")
        print("Please run `python3 auto_rater_platinum_generator.py` first to extract the dataset.\n")
        sys.exit(1)
        
    if not offline_file.exists():
        print(f"\nError: Offline emails payload file missing at {offline_file.name}\n")
        sys.exit(1)
        
    with open(platinum_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
        
    with open(offline_file, "r", encoding="utf-8") as f:
        offline_list = json.load(f)
        
    offline_by_id = {e.get("message_id"): e for e in offline_list}
    results: List[Dict[str, Any]] = payload.get("results", [])
    
    total = len(results)
    reviewed_count = sum(1 for r in results if r.get("reviewed") is True)
    
    print(f"\n📊 Platinum Human Reviewer Dashboard Initialized.")
    print(f"   Total Batch:    {total} emails")
    print(f"   Already Rated:  {reviewed_count} emails")
    print(f"   Remaining:      {total - reviewed_count} emails\n")
    
    if reviewed_count == total and total > 0:
        print("🎉 All emails in the Platinum dataset have already been reviewed!")
        print("If you wish to restart, reset the 'reviewed' fields in the JSON file.\n")
        return
        
    for idx, r in enumerate(results, 1):
        if r.get("reviewed") is True:
            continue # Skip already reviewed items instantly to resume rating
            
        msg_id = r.get("message_id")
        original = offline_by_id.get(msg_id, {})
        
        display_card(idx, total, r, original)
        
        while True:
            try:
                ans = input("Is this baseline decision correct? [y (yes) / n (no) / q (quit)]: ").strip().lower()
                if ans == 'q':
                    print("\n💾 Saving progress and exiting review dashboard. You can resume anytime!\n")
                    sys.exit(0)
                elif ans == 'y':
                    r["reviewed"] = True
                    break
                elif ans == 'n':
                    # Prompt for corrected integer level
                    while True:
                        lvl_input = input("  Enter corrected triage level integer (0, 1, or 2): ").strip()
                        if lvl_input in ['0', '1', '2']:
                            r["triage_level"] = int(lvl_input)
                            break
                        else:
                            print("  ⚠️ Invalid level. Please type exactly 0, 1, or 2.")
                            
                    # Prompt for justification rationale
                    just_input = input("  Enter correct justification rationale description: ").strip()
                    r["reason"] = just_input if just_input else "Human corrected decision"
                    r["reviewed"] = True
                    break
                else:
                    print("⚠️ Invalid input. Please type 'y', 'n', or 'q'.")
            except (KeyboardInterrupt, EOFError):
                print("\n\n💾 Saving progress and exiting review dashboard. You can resume anytime!\n")
                sys.exit(0)
                
        # Immediate Flush back to disk after every response
        payload["results"] = results
        with open(platinum_file, "w", encoding="utf-8") as out_f:
            json.dump(payload, out_f, indent=2, ensure_ascii=False)
            
        print(f"✔️ Progress saved (Email {idx} rated successfully).\n")
        
    print("\n🏆 Fantastic! All emails in the Platinum dataset have been fully reviewed and rated.\n")

if __name__ == "__main__":
    main()
