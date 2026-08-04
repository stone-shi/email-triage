import json
import logging
import argparse
import yaml
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
import httpx
import prompts_store
from config import settings
from triage import EmailTriageEngine, MAX_TOKENS_LEVEL_2, extract_json
from db import EmailDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] auto_rater_runner: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("auto_rater_runner")

# Omniroute caches identical requests; benchmarking needs a fresh completion every time.
# The header name is load-bearing and silently ignored if wrong: an unrecognized one still gets
# a 200 back, just served from cache (x-omniroute-cache: HIT, ~0.01s), which quietly turns the
# per-stage duration_sec metrics into cache-lookup times instead of real generation latency.
# Verified against this deployment: X-Omniroute-No-Cache is honored, X-Omniroute-Skip-Cache is not.
OMNIROUTE_NO_CACHE_HEADER = {"X-Omniroute-No-Cache": "true"}

# Ceiling on completion tokens for this module's own (non-EmailTriageEngine) LLM calls -- the
# Level 0 judge audit and the --test reachability probe. Sized like triage.py's constants: big
# enough that a reasoning model still reaches its JSON answer after spending most of the budget
# on hidden thinking tokens, since a mid-thought truncation returns empty content instead.
MAX_TOKENS_LEVEL_0_JUDGE = 3072
MAX_TOKENS_REACHABILITY_PROBE = 4096


def run_config(config: Dict[str, Any], emails: List[Dict[str, Any]], workspace_dir: Path, judge_model: str, level_0_judge_model: str, force_rerun: bool = False, max_items: int = None, skip_summary: bool = False) -> Dict[str, Any]:
    config_name = config["name"]
    triage_model = config["triage_model"]
    summary_model = config["summary_model"]
    
    output_file = workspace_dir / "auto_rater_data" / f"auto_rater_results_{config_name}.json"
    
    existing_results: Dict[str, Dict[str, Any]] = {}
    existing_total_duration = 0.0
    if output_file.exists() and not force_rerun:
        try:
            with open(output_file, "r", encoding="utf-8") as out_f:
                old_payload = json.load(out_f)
            existing_results = {r["message_id"]: r for r in old_payload.get("results", [])}
            existing_total_duration = old_payload.get("total_processing_all_emails_duration_sec", 0.0)
            logger.info("Incremental Mode Active: Loaded %d already processed items from cache.", len(existing_results))
        except Exception:
            existing_results = {}
            existing_total_duration = 0.0

    logger.info("==================================================")
    logger.info("Executing Test Configuration: '%s'", config_name)
    logger.info("Triage Model: %s | Summary Model: %s", triage_model, summary_model)
    logger.info("==================================================")
    
    base_url = settings.llm_base_url.rstrip('/')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}",
        **OMNIROUTE_NO_CACHE_HEADER,
    }

    # Load external prompts if present
    prompts_path = workspace_dir / "prompts.yml"
    prompts = {}
    try:
        if prompts_path.exists():
            with open(prompts_path, "r", encoding="utf-8") as f:
                prompts = yaml.safe_load(f) or {}
    except Exception:
        pass
    
    http_client = httpx.Client(timeout=1800.0)
    run_results: List[Dict[str, Any]] = []
    llm_call_log: List[Dict[str, Any]] = []
    
    # Dynamically override reranker settings for this specific configuration profile run
    old_triage_type = getattr(settings.triage, "triage_type", "llm")
    old_tei_url = getattr(settings.triage, "tei_url", "")
    old_tei_model = getattr(settings.triage, "tei_model", "")
    old_tei_api_key = getattr(settings.triage, "tei_api_key", "")
    old_tei_router_enabled = getattr(settings.triage, "tei_router_enabled", False)
    old_tei_noise_enabled = getattr(settings.triage, "tei_noise_enabled", True)
    old_tei_noise_threshold = getattr(settings.triage, "tei_noise_threshold", 0.8)

    if "triage_type" in config:
        settings.triage.triage_type = config["triage_type"]
    if "tei_url" in config:
        settings.triage.tei_url = config["tei_url"]
    if "tei_model" in config:
        settings.triage.tei_model = config["tei_model"]
    if "tei_api_key" in config:
        settings.triage.tei_api_key = config["tei_api_key"]
    if "tei_router_enabled" in config:
        settings.triage.tei_router_enabled = config["tei_router_enabled"]
    if "tei_noise_enabled" in config:
        settings.triage.tei_noise_enabled = config["tei_noise_enabled"]
    if "tei_noise_threshold" in config:
        settings.triage.tei_noise_threshold = config["tei_noise_threshold"]

    # Initialize triage engine for static filtering logic (Level 0)
    dummy_db = EmailDB(db_path=workspace_dir / "email_cache.db")
    engine = EmailTriageEngine(dummy_db)
    engine.headers.update(OMNIROUTE_NO_CACHE_HEADER)
    engine.triage_headers.update(OMNIROUTE_NO_CACHE_HEADER)
    engine.summary_headers.update(OMNIROUTE_NO_CACHE_HEADER)

    # Optional per-configuration `reasoning_effort`. Some local reasoning models never leave
    # their thinking block and burn the whole completion budget without emitting any content
    # (the proxy then reports it as a 502 empty upstream response), so they cannot be
    # benchmarked at all unless thinking is switched off for them specifically. Scoped to the
    # one configuration that declares it: it measurably changes classification decisions, so
    # applying it globally would silently alter what every other config is measuring.
    reasoning_effort = config.get("reasoning_effort")
    if reasoning_effort:
        engine.extra_payload_params["reasoning_effort"] = reasoning_effort
        logger.info("Configuration '%s' pins reasoning_effort=%r on both models.", config_name, reasoning_effort)

    new_emails_duration = 0.0
    processed_any_new = False

    l0_processed = 0
    l1_processed = 0
    l2_processed = 0

    def write_output(partial: bool) -> None:
        """Persist results so far. Called periodically, not just at the end: a slow local model
        can take minutes per email, and a run that only wrote on completion left no artifact at
        all if it was interrupted -- and no way to see how far it had gotten. Written via a temp
        file + atomic replace so an interrupt mid-write can't leave truncated JSON behind, which
        would poison the incremental-cache reload on the next run."""
        payload = {
            "configuration_name": config_name,
            "triage_model": triage_model,
            "summary_model": summary_model,
            "total_processing_all_emails_duration_sec": (
                existing_total_duration + new_emails_duration if processed_any_new else existing_total_duration
            ),
            "total_emails_processed": len(emails),
            "results": run_results,
        }
        if partial:
            payload["partial"] = True
        output_file.parent.mkdir(exist_ok=True)
        tmp_file = output_file.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as tmp_f:
            json.dump(payload, tmp_f, indent=2, ensure_ascii=False)
        tmp_file.replace(output_file)


    for idx, email in enumerate(emails, 1):
        if max_items is not None and l0_processed >= max_items and l1_processed >= max_items and l2_processed >= max_items:
            logger.info("Max items reached for all levels. Stopping processing.")
            break
        sender = email["sender"]
        subject = email["subject"]
        snippet = email["snippet"]
        full_body = email["full_body"]
        msg_id = email["message_id"]
        
        # Cache Skip Layer Conditional Check: Unchanged Model + Message ID cached match
        if msg_id in existing_results and not force_rerun:
            run_results.append(existing_results[msg_id])
            continue
            
        email_start_time = time.time()
        
        # Initialize default metrics record matching user requirements
        metrics = {
            "triage_level": 0,
            "message_id": msg_id,
            "account": email["account"],
            "sender": sender,
            "subject": subject,
            "date": email["date"],
            "reason": "Passed static filter",
            "summary": None,
            "score": 1.0,
            "tag": "notification",
            "model_used_triage": triage_model,
            "model_used_summary": summary_model,
            "level_1_duration_sec": 0.0,
            "level_2_duration_sec": 0.0,
            "level_1_prompt_tokens": 0,
            "level_1_completion_tokens": 0,
            "level_2_prompt_tokens": 0,
            "level_2_completion_tokens": 0,
            "total_email_process_duration_sec": 0.0,
            "level_0_judge_correctness": "N/A",
            "level_0_judge_score": 1.0,
            "level_0_judge_reason": None
        }
        
        # VIP Whitelist Override Layer -> Direct to Level 2
        if engine.is_vip_sender(sender):
            if max_items is not None and l2_processed >= max_items:
                continue
            l2_processed += 1
            logger.info("VIP hit: Sender '%s' is a whitelisted VIP. Bypassing Level 0 and Level 1 directly to Level 2!", sender)
            metrics["triage_level"] = 2
            metrics["reason"] = "VIP Sender Direct Escalation"
            metrics["tag"] = "vip"

            if skip_summary:
                pass  # leave summary as None -- filter-quality-only run
            elif not full_body or len(full_body.strip()) < 10:
                metrics["summary"] = "No substantive content to summarize."
            else:
                l2_prompt = f"Subject: {subject}\nBody:\n{full_body[:8000]}"
                # Fall back to the hardcoded default like triage.py's run_level_* methods do --
                # never send an empty system prompt, which is what happened silently whenever
                # prompts.yml was absent or unparseable and left `prompts` an empty dict.
                l2_system = prompts.get("level_2_summarization", {}).get("system") or prompts_store.DEFAULT_PROMPTS["level_2_summarization"]
                l2_start = time.time()
                try:
                    l2_payload = {
                        "model": summary_model,
                        "messages": [
                            {"role": "system", "content": l2_system},
                            {"role": "user", "content": l2_prompt}
                        ],
                        "temperature": 0.2,
                        "include_reasoning": False,
                        "stream": False,
                        "max_tokens": MAX_TOKENS_LEVEL_2,
                        **engine.extra_payload_params,
                    }
                    resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l2_payload)
                    resp.raise_for_status()
                    resp_json = resp.json()
                    
                    usage = resp_json.get("usage", {})
                    metrics["level_2_prompt_tokens"] = usage.get("prompt_tokens", 0)
                    metrics["level_2_completion_tokens"] = usage.get("completion_tokens", 0)
                    
                    content = resp_json["choices"][0]["message"]["content"]
                    result_dict = json.loads(extract_json(content))
                    
                    metrics["summary"] = result_dict.get("summary", "")
                    metrics["score"] = result_dict.get("confidence_score", 1.0)
                    metrics["tag"] = result_dict.get("tag", "vip")
                    llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_2_vip_summary", "status": "success", "error": None})
                except Exception as e:
                    logger.error("Level 2 VIP summary failed for email %s: %s", msg_id, e)
                    metrics["summary"] = f"Level 2 summarization error: {str(e)}"
                    llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_2_vip_summary", "status": "error", "error": str(e)})

                metrics["level_2_duration_sec"] = time.time() - l2_start
                
            metrics["total_email_process_duration_sec"] = time.time() - email_start_time
            run_results.append(metrics)
            continue

        # 1. Level 0 Static Filter
        is_noise, l0_reason = engine.run_level_0_static(sender, subject)
        if is_noise:
            if max_items is not None and l0_processed >= max_items:
                continue
            l0_processed += 1
            metrics["triage_level"] = 0
            metrics["reason"] = l0_reason
            metrics["tag"] = "low"
            
            # Use judge_model to verify if the Level 0 filter was actually correct
            l0_audit_prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
            l0_audit_system = prompts.get("auto_rater_level_0_audit", {}).get("system")
            if not l0_audit_system:
                l0_audit_system = (
                    "You are an expert email auditor. Review the email metadata to determine if it is truly low priority noise "
                    "(e.g., automated notifications, transactional marketing, newsletters, spam) or if it was a false positive "
                    "that actually contains high priority business communication or a critical personal update.\n"
                    "You MUST return a valid JSON object containing exactly three fields: "
                    "'is_actually_low_priority' (boolean), 'reason' (string), and 'confidence_score' (float from 0.0 to 1.0)."
                )
            try:
                l0_payload = {
                    "model": level_0_judge_model,
                    "messages": [
                        {"role": "system", "content": l0_audit_system},
                        {"role": "user", "content": l0_audit_prompt}
                    ],
                    "temperature": 0.0,
                    "include_reasoning": False,
                    "stream": False,
                    "max_tokens": MAX_TOKENS_LEVEL_0_JUDGE,
                }
                resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=l0_payload)
                resp.raise_for_status()
                audit_dict = json.loads(extract_json(resp.json()["choices"][0]["message"]["content"]))
                
                metrics["level_0_judge_correctness"] = "Correct" if audit_dict.get("is_actually_low_priority", True) else "False Positive"
                metrics["level_0_judge_score"] = audit_dict.get("confidence_score", 1.0)
                metrics["level_0_judge_reason"] = audit_dict.get("reason", "")
                llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_0_judge_audit", "status": "success", "error": None})
            except Exception as audit_err:
                logger.error("Level 0 judge audit failed: %s", audit_err)
                metrics["level_0_judge_correctness"] = "Audit Failed"
                metrics["level_0_judge_score"] = 0.0
                metrics["level_0_judge_reason"] = str(audit_err)
                llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_0_judge_audit", "status": "error", "error": str(audit_err)})
                continue # Skip caching if judge audit failed

            metrics["total_email_process_duration_sec"] = time.time() - email_start_time
            new_emails_duration += metrics["total_email_process_duration_sec"]
            processed_any_new = True
            run_results.append(metrics)
            continue
            
        # 1.5 Level 0.5 rerank noise filter (never escalates, only ever short-circuits to Level 0)
        tei_override_level, tei_reason, tei_score = engine.run_rerank_router(sender, subject, snippet)

        if tei_override_level == 0:
            if max_items is not None and l0_processed >= max_items:
                continue
            l0_processed += 1
            metrics["triage_level"] = 0
            metrics["reason"] = tei_reason
            metrics["score"] = tei_score
            metrics["tag"] = "low"
            metrics["total_email_process_duration_sec"] = time.time() - email_start_time
            new_emails_duration += metrics["total_email_process_duration_sec"]
            processed_any_new = True
            run_results.append(metrics)
            continue
            
        # 2. Level 1 LLM / rerank classifier ingestion
        if max_items is not None and l1_processed >= max_items:
            continue
        l1_processed += 1
        suggested_level, reason, score, l1_tag, l1_metrics = engine.run_level_1_classification(sender, subject, snippet, model_name=triage_model)
        
        metrics["reason"] = reason
        metrics["score"] = score
        metrics["tag"] = l1_tag
        metrics["triage_level"] = suggested_level
        metrics["level_1_duration_sec"] = l1_metrics["duration_sec"]
        metrics["level_1_prompt_tokens"] = l1_metrics["prompt_tokens"]
        metrics["level_1_completion_tokens"] = l1_metrics["completion_tokens"]
        
        # Check for endpoint failures to support resume capability
        if "Proxy error:" in reason or "Rerank server prediction error:" in reason:
            logger.warning("Omitting email %s from results cache due to runtime LLM endpoint error.", msg_id)
            llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_1_classification", "status": "error", "error": reason})
            continue
        llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_1_classification", "status": "success", "error": None})


        # 3. Level 2 Premium Summary (only if Level 1 suggested Level 2)
        if suggested_level == 2:
            if max_items is not None and l2_processed >= max_items:
                # Skip L2 summary to save time, keep as Level 1
                metrics["triage_level"] = 1
            else:
                l2_processed += 1
                metrics["triage_level"] = 2
                if skip_summary:
                    pass  # leave summary as None -- filter-quality-only run
                elif not full_body or len(full_body.strip()) < 10:
                    metrics["summary"] = "No substantive content to summarize."
                else:
                    summary, summary_score, l2_tag, l2_metrics = engine.run_level_2_summarization(subject, full_body, model_name=summary_model)
                    
                    # Check for endpoint failures
                    if "Failed to generate proxy summary due to error" in summary:
                        logger.warning("Omitting email %s from results cache due to Level 2 summarization failure.", msg_id)
                        llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_2_summarization", "status": "error", "error": summary})
                        continue

                    metrics["summary"] = summary
                    metrics["score"] = summary_score
                    metrics["tag"] = l2_tag
                    metrics["level_2_duration_sec"] = l2_metrics["duration_sec"]
                    metrics["level_2_prompt_tokens"] = l2_metrics["prompt_tokens"]
                    metrics["level_2_completion_tokens"] = l2_metrics["completion_tokens"]
                    llm_call_log.append({"message_id": msg_id, "subject": subject, "stage": "level_2_summarization", "status": "success", "error": None})
                
        metrics["total_email_process_duration_sec"] = time.time() - email_start_time
        new_emails_duration += metrics["total_email_process_duration_sec"]
        processed_any_new = True
        run_results.append(metrics)

        # Progress + running average, so a config grinding on a slow local model is visibly
        # making progress rather than looking hung for hours with no output at all.
        newly_done = l0_processed + l1_processed + l2_processed
        avg = new_emails_duration / max(newly_done, 1)
        logger.info(
            "[%s] %d/%d done (level %s, %.1fs; avg %.1fs/email, ~%.0f min left for %d remaining)",
            config_name, idx, len(emails), metrics["triage_level"],
            metrics["total_email_process_duration_sec"], avg,
            avg * (len(emails) - idx) / 60.0, len(emails) - idx,
        )
        if newly_done % 5 == 0:
            write_output(partial=True)


    # Final write: same payload as the periodic ones, minus the "partial" marker.
    write_output(partial=False)


    # Restore old reranker settings to preserve clean state across profile loop iterations
    settings.triage.triage_type = old_triage_type
    settings.triage.tei_url = old_tei_url
    settings.triage.tei_model = old_tei_model
    settings.triage.tei_api_key = old_tei_api_key
    settings.triage.tei_router_enabled = old_tei_router_enabled
    settings.triage.tei_noise_enabled = old_tei_noise_enabled
    settings.triage.tei_noise_threshold = old_tei_noise_threshold
        
    llm_success_count = sum(1 for c in llm_call_log if c["status"] == "success")
    llm_error_count = len(llm_call_log) - llm_success_count
    logger.info("Finished test run for '%s'. Results saved pretty to %s (LLM calls: %d success, %d error)", config_name, output_file, llm_success_count, llm_error_count)

    return {"config_name": config_name, "llm_calls": llm_call_log}

def test_llm_reachability(model_name: str, base_url: str, headers: Dict[str, str], http_client: httpx.Client, reasoning_effort: Optional[str] = None) -> Tuple[bool, str]:
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Write a detailed paragraph (at least 100 words) explaining what an email triage pipeline does."}
        ],
        "max_tokens": MAX_TOKENS_REACHABILITY_PROBE,
        "temperature": 0.2,
        "include_reasoning": False,
        # Must match what the real pipeline sends: the proxy streams Server-Sent Events unless
        # streaming is explicitly declined, and resp.json() cannot parse an SSE body.
        "stream": False,
    }
    # Probe the model the same way its configuration will actually drive it, otherwise a model
    # that only answers with thinking disabled is reported as unreachable here while working
    # fine in the real run.
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    try:
        resp = http_client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        if "text/event-stream" in resp.headers.get("content-type", ""):
            return False, "proxy returned a streaming (SSE) response despite stream=false -- the pipeline cannot parse it"
        try:
            resp_json = resp.json()
        except json.JSONDecodeError:
            return False, f"reachable but response body is not JSON (content-type {resp.headers.get('content-type')!r})"
        usage = resp_json.get("usage", {})
        completion_tokens = usage.get("completion_tokens", 0)
        choices = resp_json.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        # A reasoning model can burn the whole budget on hidden thinking tokens and still return
        # empty content, so completion_tokens alone does not prove the model produced an answer.
        if not content.strip():
            return False, f"reachable but returned empty content ({completion_tokens} completion tokens, likely all reasoning -- raise max_tokens)"
        if completion_tokens <= 64:
            return False, f"reachable but only returned {completion_tokens} completion tokens (need > 64)"
        return True, f"reachable, {completion_tokens} completion tokens returned"
    except Exception as e:
        return False, f"unreachable or request failed: {e}"

def main() -> None:
    workspace_dir = Path(__file__).parent.resolve()
    config_path = workspace_dir / "auto_rater_config.yml"
    emails_path = workspace_dir / "auto_rater_data" / "offline_emails.json"
    
    if not config_path.exists() or not emails_path.exists():
        logger.error("Required files missing. Make sure config and offline_emails.json exist.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    # Dynamically calibrate logging thresholds
    log_level = config_data.get("log_level", "INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    logger.setLevel(numeric_level)
        
    with open(emails_path, "r") as f:
        emails = json.load(f)
        
    configs = config_data.get("test_configurations", [])
    judge_model = config_data.get("judge_model", "deepseek/deepseek-v4-pro")
    level_0_judge_model = config_data.get("level_0_judge_model", judge_model)
    if not configs:
        logger.error("No test configurations found in config file.")
        sys.exit(1)
        
    parser = argparse.ArgumentParser(description="Auto Rater Benchmarking Runner")
    parser.add_argument("--run", type=str, help="Name of a single test configuration pair to execute specifically")
    parser.add_argument("-f", "--force", action="store_true", help="Force execution and overwrite existing benchmark results file")
    parser.add_argument("--max-items", type=int, help="Maximum items to process per triage level tier (useful for fast testing)")
    parser.add_argument("--skip-summary", action="store_true", help="Skip Level 2 summarization entirely -- use when you only want to check triage/filter quality, not summary quality")
    parser.add_argument("--only-missing", action="store_true", help="When --run is not given, only run configurations that don't already have a results JSON file")
    parser.add_argument("--test", action="store_true", help="Test each configuration's triage_model/summary_model for LLM reachability and completion_tokens > 64, then exit without running the benchmark")
    args = parser.parse_args()

    if args.skip_summary:
        logger.info("--skip-summary active: Level 2 summaries will not be generated (summary field stays null).")
    
    if args.run:
        configs = [c for c in configs if c.get("name") == args.run]
        if not configs:
            logger.error("No test configuration found matching name: '%s'", args.run)
            sys.exit(1)
        logger.info("Targeted single configuration run: '%s'", args.run)
    elif args.only_missing:
        pending = [c for c in configs if not (workspace_dir / "auto_rater_data" / f"auto_rater_results_{c.get('name')}.json").exists()]
        skipped = len(configs) - len(pending)
        if skipped:
            logger.info("--only-missing active: skipping %d configuration(s) that already have results.", skipped)
        configs = pending
        if not configs:
            logger.info("No configurations pending -- all already have results.")
            return

    if args.test:
        base_url = settings.llm_base_url.rstrip('/')
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
            **OMNIROUTE_NO_CACHE_HEADER,
        }
        http_client = httpx.Client(timeout=120.0)
        model_results: Dict[Tuple[Optional[str], Optional[str]], Tuple[bool, str]] = {}
        all_passed = True
        logger.info("=" * 60)
        logger.info("LLM CONNECTIVITY TEST")
        logger.info("=" * 60)
        for cfg in configs:
            cfg_name = cfg.get("name")
            logger.info("Configuration '%s':", cfg_name)
            cfg_effort = cfg.get("reasoning_effort")
            for role in ("triage_model", "summary_model"):
                model_name = cfg.get(role)
                # Key the memo on the effort too: the same model can fail without it and pass
                # with it, so a shared entry would report whichever config was probed first.
                cache_key = (model_name, cfg_effort)
                if cache_key not in model_results:
                    model_results[cache_key] = test_llm_reachability(model_name, base_url, headers, http_client, cfg_effort)
                passed, detail = model_results[cache_key]
                all_passed = all_passed and passed
                suffix = f" [reasoning_effort={cfg_effort}]" if cfg_effort else ""
                logger.info("    [%s] %s (%s)%s - %s", "PASS" if passed else "FAIL", model_name, role, suffix, detail)
        logger.info("=" * 60)
        logger.info("LLM connectivity test %s", "PASSED" if all_passed else "FAILED")
        logger.info("=" * 60)
        sys.exit(0 if all_passed else 1)

    logger.info("Loaded %d offline emails. Starting benchmarking configurations...", len(emails))

    run_summaries: List[Dict[str, Any]] = []

    for cfg in configs:
        cfg_name = cfg.get("name")
        triage_model = cfg.get("triage_model")
        summary_model = cfg.get("summary_model")
        output_file = workspace_dir / "auto_rater_data" / f"auto_rater_results_{cfg_name}.json"

        if output_file.exists():
            try:
                with open(output_file, "r", encoding="utf-8") as out_f:
                    existing_data = json.load(out_f)
            except Exception:
                existing_data = {}
            # 2. Model Definition Modifications Guard Abort Check
            if existing_data.get("triage_model") != triage_model or existing_data.get("summary_model") != summary_model:
                if not args.force:
                    logger.error("⚠️ WARNING: Model configuration strings changed for profile '%s' (Triage: %s -> %s, Summary: %s -> %s). Execution aborted to protect data integrity. Use -f/--force to override and overwrite.", cfg_name, existing_data.get("triage_model"), triage_model, existing_data.get("summary_model"), summary_model)
                    sys.exit(1)
                logger.info("Force override active: Overwriting modified model pairs for configuration '%s'...", cfg_name)

        try:
            result = run_config(cfg, emails, workspace_dir, judge_model, level_0_judge_model, force_rerun=args.force, max_items=args.max_items, skip_summary=args.skip_summary)
            run_summaries.append({"config_name": cfg_name, "status": "completed", "llm_calls": result.get("llm_calls", [])})
        except Exception as e:
            logger.error("Configuration run failed for %s: %s", cfg_name, e)
            run_summaries.append({"config_name": cfg_name, "status": "failed", "error": str(e), "llm_calls": []})
            continue

    logger.info("=" * 60)
    logger.info("RUN SUMMARY")
    logger.info("=" * 60)
    logger.info("Configurations run: %s", ", ".join(s["config_name"] for s in run_summaries) or "(none)")
    for s in run_summaries:
        if s["status"] == "failed":
            logger.info("Configuration '%s': FAILED to run -- %s", s["config_name"], s["error"])
            continue
        calls = s["llm_calls"]
        success = sum(1 for c in calls if c["status"] == "success")
        error = len(calls) - success
        logger.info("Configuration '%s': completed -- %d LLM call(s) succeeded, %d failed", s["config_name"], success, error)
        for c in calls:
            if c["status"] == "success":
                logger.info("    [OK]    %s (%s) - %s", c["message_id"], c["stage"], c["subject"])
            else:
                logger.info("    [ERROR] %s (%s) - %s: %s", c["message_id"], c["stage"], c["subject"], c["error"])
    logger.info("=" * 60)

if __name__ == "__main__":
    main()
