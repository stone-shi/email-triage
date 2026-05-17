import json
import logging
import sys
import re
from pathlib import Path
from typing import Dict, Any, List
import httpx
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] platinum_generator: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("platinum_generator")
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

def evenly_spaced_sampling(items: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    if len(items) <= count:
        return items
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = (workspace_dir / "auto_rater_data").resolve()
    
    source_file = data_dir / "auto_rater_results_baseline_deepseek_pro.json"
    output_file = data_dir / "auto_rater_results_baseline_platinum_human.json"
    
    if not source_file.exists():
        logger.error("Source file %s does not exist.", source_file)
        sys.exit(1)
        
    with open(source_file, "r", encoding="utf-8") as f:
        source_payload = json.load(f)
        
    results = source_payload.get("results", [])
    logger.info("Loaded %d records from baseline_deepseek_pro.", len(results))
    
    results_by_level: Dict[int, List[Dict[str, Any]]] = {0: [], 1: [], 2: []}
    for r in results:
        lvl = r.get("triage_level")
        if isinstance(lvl, int) and lvl in results_by_level:
            results_by_level[lvl].append(r)
            
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}"
    }
    http_client = httpx.Client(timeout=1800.0)
    cluster_model = "deepseek/deepseek-v4-flash"
    
    platinum_results: List[Dict[str, Any]] = []
    
    for lvl, items in results_by_level.items():
        logger.info("==================================================")
        logger.info("Processing Level %d (Total items: %d)", lvl, len(items))
        
        if len(items) <= 10:
            logger.info("Items count (%d) <= 10. Retaining all items directly.", len(items))
            selected_items = items
        else:
            # Step 1: Algorithmic initial grouping / subsetting to prevent token limit blowout
            # We take up to 100 evenly spaced items to represent the full distribution
            subset = evenly_spaced_sampling(items, min(len(items), 100))
            
            # Prepare JSON list payload for LLM clustering
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
                f"Analyze their metadata (sender, subject, reason) to group them into unique thematic/topic categories "
                f"(merging identical automated senders, repeated promotional newsletters, or overlapping notifications).\n"
                f"Count the total number of unique categories you discovered.\n"
                f"- If the total number of unique categories is between 10 and 19 (inclusive), select exactly ONE representative item_id from every category to preserve all distinct categories.\n"
                f"- If the total number of unique categories is 20 or more, or less than 10, select exactly 10 highly diverse representative item_id values that cover the most important distinct categories.\n\n"
                f"You MUST return a valid JSON object containing exactly three keys:\n"
                f"1. 'total_unique_categories_found': integer count,\n"
                f"2. 'category_descriptions': list of string summaries describing each discovered category,\n"
                f"3. 'selected_representative_item_ids': list of integer item_id choices."
            )
            
            user_content = json.dumps(items_for_llm, ensure_ascii=False)
            
            payload = {
                "model": cluster_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "temperature": 0.0,
                "include_reasoning": False
            }
            
            logger.info("Invoking %s clustering call for Level %d...", cluster_model, lvl)
            try:
                resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                
                parsed = json.loads(extract_json(content))
                cat_count = parsed.get("total_unique_categories_found", 0)
                selected_ids = parsed.get("selected_representative_item_ids", [])
                
                logger.info("LLM discovered %d unique categories.", cat_count)
                logger.info("LLM selected %d representative indices: %s", len(selected_ids), selected_ids)
                
                selected_items = []
                for i_id in selected_ids:
                    if isinstance(i_id, int) and 0 <= i_id < len(subset):
                        selected_items.append(subset[i_id])
                        
                if not selected_items:
                    raise ValueError("Empty selection returned by LLM.")
                    
            except Exception as e:
                logger.warning("LLM clustering failed or returned malformed JSON (%s). Falling back to algorithmic even spacing.", e)
                selected_items = evenly_spaced_sampling(items, 10)
                
        # Inject reviewed flag and append to final platinum dataset container
        for rec in selected_items:
            # Create a deep copy to isolate fields
            rec_copy = dict(rec)
            rec_copy["reviewed"] = False
            platinum_results.append(rec_copy)
            
    output_payload = {
        "configuration_name": "baseline_platinum_human",
        "triage_model": "human_reviewer",
        "summary_model": "human_reviewer",
        "total_processing_all_emails_duration_sec": 0.0,
        "total_emails_processed": len(platinum_results),
        "results": platinum_results
    }
    
    with open(output_file, "w", encoding="utf-8") as out_f:
        json.dump(output_payload, out_f, indent=2, ensure_ascii=False)
        
    logger.info("==================================================")
    logger.info("Successfully extracted Platinum Dataset (%d total emails) to %s", len(platinum_results), output_file.name)

if __name__ == "__main__":
    main()
