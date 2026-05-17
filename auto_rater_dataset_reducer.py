import json
import logging
import sys
import re
import shutil
from pathlib import Path
from typing import Dict, Any, List
import httpx
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] dataset_reducer: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("dataset_reducer")
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

def evenly_spaced_sampling(items: List[Any], count: int) -> List[Any]:
    if len(items) <= count:
        return items
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]

def enforce_exact_count(selected_ids: List[int], total_items: int, target_count: int) -> List[int]:
    valid_ids = []
    for idx in selected_ids:
        if isinstance(idx, int) and 0 <= idx < total_items and idx not in valid_ids:
            valid_ids.append(idx)
            
    if len(valid_ids) == target_count:
        return valid_ids
        
    if len(valid_ids) > target_count:
        step = len(valid_ids) / target_count
        return [valid_ids[int(i * step)] for i in range(target_count)]
        
    unselected = [i for i in range(total_items) if i not in valid_ids]
    needed = target_count - len(valid_ids)
    if needed > 0 and unselected:
        step = len(unselected) / needed
        pad_ids = [unselected[int(i * step)] for i in range(needed)]
        valid_ids.extend(pad_ids)
        
    return valid_ids[:target_count]

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = (workspace_dir / "auto_rater_data").resolve()
    
    source_file = data_dir / "auto_rater_results_baseline_deepseek_pro.json"
    offline_file = data_dir / "offline_emails.json"
    
    if not source_file.exists() or not offline_file.exists():
        logger.error("Required files baseline_deepseek_pro or offline_emails.json missing.")
        sys.exit(1)
        
    # Step 0: Safe physical backup of every JSON file to prevent data loss
    backup_dir = (workspace_dir / "auto_rater_data_backup").resolve()
    backup_dir.mkdir(exist_ok=True)
    logger.info("==================================================")
    logger.info("Creating safe physical backup of datasets to %s...", backup_dir.name)
    
    backup_count = 0
    for f_path in data_dir.glob("*.json"):
        dest = backup_dir / f_path.name
        shutil.copy2(f_path.resolve(), dest)
        backup_count += 1
    logger.info("Backed up %d JSON files successfully.", backup_count)
    logger.info("==================================================")
    
    with open(source_file, "r", encoding="utf-8") as f:
        source_payload = json.load(f)
        
    results = source_payload.get("results", [])
    logger.info("Loaded %d total records from baseline configuration.", len(results))
    
    results_by_level: Dict[int, List[Dict[str, Any]]] = {0: [], 1: [], 2: []}
    for r in results:
        lvl = r.get("triage_level")
        if isinstance(lvl, int) and lvl in results_by_level:
            results_by_level[lvl].append(r)
            
    # Dynamic quota calculations matching user constraints exactly
    n0 = min(len(results_by_level[0]), 15)
    n2 = min(len(results_by_level[2]), 15)
    n1 = 100 - n0 - n2
    
    quotas = {0: n0, 1: n1, 2: n2}
    logger.info("Allocated quotas to sum exactly 100 emails:")
    logger.info(" - Level 0 Quota: %d", n0)
    logger.info(" - Level 1 Quota: %d", n1)
    logger.info(" - Level 2 Quota: %d", n2)
    logger.info("==================================================")
    
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}"
    }
    http_client = httpx.Client(timeout=1800.0)
    cluster_model = "deepseek/deepseek-v4-flash"
    
    master_selected_message_ids: List[str] = []
    
    for lvl, target_n in quotas.items():
        items = results_by_level[lvl]
        logger.info("Reducing Level %d (Available: %d, Target Quota: %d)", lvl, len(items), target_n)
        
        if len(items) <= target_n:
            logger.info("Available items (%d) <= target quota (%d). Keeping all available.", len(items), target_n)
            final_subset = items
        else:
            # Algorithmic initial cap to fit LLM token context safely
            subset = evenly_spaced_sampling(items, min(len(items), 120))
            
            items_for_llm = []
            for idx, it in enumerate(subset):
                items_for_llm.append({
                    "item_id": idx,
                    "sender": it.get("sender"),
                    "subject": it.get("subject"),
                    "reason": it.get("reason")
                })
                
            system_prompt = (
                f"You are an expert data scientist curating a highly representative evaluation dataset. "
                f"Review the provided JSON list of emails belonging to Triage Level {lvl}.\n"
                f"Analyze their metadata (sender, subject, reason) to group them into unique thematic/topic categories.\n"
                f"Your STRICT goal is to extract exactly {target_n} highly diverse representative item_id values that maximize breadth of coverage across the entire dataset timeline, distinct senders, and topics.\n"
                f"You MUST return a valid JSON object containing exactly two keys:\n"
                f"1. 'category_descriptions': list of string descriptions for discovered categories,\n"
                f"2. 'selected_representative_item_ids': list of exactly {target_n} integer item_id values."
            )
            
            user_content = json.dumps(items_for_llm, ensure_ascii=False)
            payload = {
                "model": cluster_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "temperature": 0.0
            }
            
            logger.info("Invoking %s proxy clustering for Level %d...", cluster_model, lvl)
            try:
                resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                
                parsed = json.loads(extract_json(content))
                selected_ids = parsed.get("selected_representative_item_ids", [])
                
                logger.info("LLM returned %d indices.", len(selected_ids))
                
                # Guarantee exactly target_n count safely
                exact_ids = enforce_exact_count(selected_ids, len(subset), target_n)
                logger.info("Enforced exact target indices count (%d): %s", len(exact_ids), exact_ids)
                
                final_subset = [subset[i] for i in exact_ids]
                
            except Exception as e:
                logger.warning("LLM reduction call failed (%s). Falling back to algorithmic even spacing.", e)
                final_subset = evenly_spaced_sampling(items, target_n)
                
        for item in final_subset:
            m_id = item.get("message_id")
            if m_id and m_id not in master_selected_message_ids:
                master_selected_message_ids.append(m_id)
                
    logger.info("==================================================")
    logger.info("Master reduction complete. Selected exactly %d unique message_ids.", len(master_selected_message_ids))
    logger.info("==================================================")
    
    if len(master_selected_message_ids) != 100:
        logger.error("CRITICAL: Master set size is %d instead of exactly 100. Aborting disk writes.", len(master_selected_message_ids))
        sys.exit(1)
        
    # Step 1: Prune offline_emails.json master db
    logger.info("Pruning master offline_emails.json database...")
    with open(offline_file, "r", encoding="utf-8") as f:
        offline_list = json.load(f)
        
    pruned_offline = [e for e in offline_list if e.get("message_id") in master_selected_message_ids]
    
    with open(offline_file, "w", encoding="utf-8") as out_f:
        json.dump(pruned_offline, out_f, indent=2, ensure_ascii=False)
    logger.info("Successfully reduced offline_emails.json to exactly %d records.", len(pruned_offline))
    
    # Step 2: Prune all existing auto_rater_results_*.json profiling files and update durations
    logger.info("==================================================")
    logger.info("Pruning all result JSON profiles and updating processing durations...")
    
    result_files = list(data_dir.glob("auto_rater_results_*.json"))
    updated_profiles = 0
    
    for r_file in result_files:
        try:
            with open(r_file, "r", encoding="utf-8") as f:
                r_payload = json.load(f)
                
            r_items = r_payload.get("results", [])
            pruned_items = [it for it in r_items if it.get("message_id") in master_selected_message_ids]
            
            # Sum individual email process times to keep total process latency accurate
            new_total_duration = sum(float(it.get("total_email_process_duration_sec", 0.0)) for it in pruned_items)
            
            r_payload["results"] = pruned_items
            r_payload["total_emails_processed"] = len(pruned_items)
            r_payload["total_processing_all_emails_duration_sec"] = new_total_duration
            
            with open(r_file, "w", encoding="utf-8") as out_f:
                json.dump(r_payload, out_f, indent=2, ensure_ascii=False)
                
            logger.info("Updated profile %s (Records: %d, New Latency: %.2fs)", r_file.name, len(pruned_items), new_total_duration)
            updated_profiles += 1
        except Exception as e:
            logger.error("Failed to prune profile %s: %s", r_file.name, e)
            
    logger.info("==================================================")
    logger.info("Spectacular reduction completed! Fully reduced %d benchmarking profiles to exactly 100 emails.", updated_profiles)

if __name__ == "__main__":
    main()
