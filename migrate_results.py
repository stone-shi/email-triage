import json
from pathlib import Path
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] migrate_results: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("migrate_results")

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    data_dir = workspace_dir / "auto_rater_data"
    
    if not data_dir.exists():
        logger.error("Data directory %s does not exist.", data_dir)
        sys.exit(1)
        
    result_files = list(data_dir.glob("auto_rater_results_*.json"))
    if not result_files:
        logger.warning("No result files found matching auto_rater_data/auto_rater_results_*.json")
        return
        
    logger.info("Starting migration of %d benchmark result files...", len(result_files))
    
    total_migrated_records = 0
    
    for p in result_files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                payload = json.load(f)
                
            results = payload.get("results", [])
            changed = False
            file_records_migrated = 0
            
            for r in results:
                lvl = r.get("triage_level")
                if isinstance(lvl, str):
                    lvl_str = lvl.strip()
                    if lvl_str.startswith("Level 0"):
                        r["triage_level"] = 0
                        changed = True
                        file_records_migrated += 1
                    elif lvl_str.startswith("Level 1"):
                        r["triage_level"] = 1
                        changed = True
                        file_records_migrated += 1
                    elif lvl_str.startswith("Level 2"):
                        r["triage_level"] = 2
                        changed = True
                        file_records_migrated += 1
                        
            if changed:
                with open(p, "w", encoding="utf-8") as out_f:
                    json.dump(payload, out_f, indent=2, ensure_ascii=False)
                logger.info("Migrated file %s successfully (%d records updated).", p.name, file_records_migrated)
                total_migrated_records += file_records_migrated
            else:
                logger.info("File %s requires no migration (already up to date).", p.name)
                
        except Exception as e:
            logger.error("Failed to migrate file %s: %s", p.name, e)
            
    logger.info("Migration completed. Total records updated across all datasets: %d", total_migrated_records)

if __name__ == "__main__":
    main()
