# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A token-efficient, multi-stage email ingestion and triage pipeline. It concurrently pulls unread mail from Gmail (API) and IMAP (Zoho), filters noise for free, classifies the rest with a cheap LLM, and escalates only high-priority items to a premium LLM for executive summaries. Results are cached in SQLite so repeated runs cost zero tokens/network calls for already-seen messages. It's exposed both as a CLI (`main.py` / `email-triage.sh`) and as an MCP server (`mcp_server.py`) for AI agents/editors.

## Commands

```bash
# Setup + run full test suite (creates venv, installs deps, runs pytest, writes JUnit XML to test-reports/)
./test.sh
./test.sh -k test_name          # pass args straight through to pytest, e.g. run a single test
./test.sh tests/test_triage.py  # run a single test file

# Run the CLI directly (uses venv/bin/python3 if venv exists, else system python3)
./email-triage.sh                       # silent JSON-array output to stdout
./email-triage.sh --pretty              # indented JSON
./email-triage.sh --human               # rich terminal UI, no JSON
./email-triage.sh --max 5 --days 3      # cap and date-filter unread scan
./email-triage.sh --level 2 --compact   # only Level 2 items, minified schema (for LLM agent consumption)
./email-triage.sh --profile stone       # run against a named multi-tenant profile (profiles/<name>/)
./email-triage.sh --auth --headless     # re-run Gmail OAuth (headless: link/redirect via stderr/stdin)
./email-triage.sh --mark-read-level 0   # mark cached Level-0 unread emails as read

# MCP server (stdio by default; set EMAIL_TRIAGE_MCP_TRANSPORT=sse for HTTP/SSE)
./venv/bin/python3 mcp_server.py

# Docker build/push (registry.shifamily.com/homestack/email-triage)
./build.sh [-t <tag>]

# Auto Rater benchmarking suite (side-by-side model comparison against gold-standard tags)
./venv/bin/python3 auto_rater_downloader.py     # pull unread messages offline into plain-text dataset
./venv/bin/python3 auto_rater_runner.py         # run every config in auto_rater_config.yml through the pipeline
./venv/bin/python3 auto_rater_triage.py         # precision/recall/F1/tag-accuracy vs. human gold labels
./venv/bin/python3 auto_rater_summarizer.py     # LLM-as-judge scoring (1-10) of Level 2 summaries
./venv/bin/python3 add_missing_tags.py --profile <name>   # backfill missing tags in an existing dataset

# Standalone classifier tester (HTML report comparing classifier configs on a dataset)
./venv/bin/python3 classifier_tester.py
```

There is no separate lint/typecheck command configured; `test.sh` is the source of truth for CI-equivalent verification.

## Architecture

### Tiered triage pipeline (`triage.py` — `EmailTriageEngine`)

Every unread email is evaluated in escalating order until a level is decided; each stage after Level 0 costs more tokens/latency, so cheaper checks always run first:

1. **VIP bypass**: sender matches `triage.whitelist_vip_senders` → skip straight to Level 2 (full body fetch + premium summary).
2. **Level 0 (static, free)**: regex/substring match against `triage.blacklist_keywords` / `blacklist_senders`, unless the sender's domain is in `whitelist_domains`. Hit → tag `"low"`, level 0.
3. **Level 0.5 (reranker semantic router, optional)**: if `triage.tei_router_enabled`, the email is reranked (via a Cohere/Jina-style `/rerank` endpoint at `triage.tei_url`, using `triage.tei_model`/`triage.tei_api_key`) against two fixed anchor documents ("important" vs "noise", `RERANK_IMPORTANT_ANCHOR`/`RERANK_NOISE_ANCHOR` in `triage.py`). The higher-scoring anchor's relevance score is compared against `tei_noise_threshold` / `tei_signal_threshold` to short-circuit straight to noise (level 0) or straight to summarization (level 2); ambiguous results fall through to Level 1. This stage still goes by "TEI" in code/config for historical reasons, but the actual backend is a reranker, not a TEI sequence-classifier — the request/response shape is `{"model", "query", "documents"}` → `{"results": [{"index", "relevance_score"}, ...]}`, not TEI's older `{"inputs"}` → `[{"label", "score"}, ...]`.
4. **Level 1 (cheap LLM ternary classification)**: sends From/Subject/Snippet only (no body fetch) to `settings.triage_model` via an OpenAI-compatible `/chat/completions` proxy, expecting strict JSON (`TriageDecision` Pydantic model: `suggested_level` 0/1/2, `reason`, `confidence_score`, `tag`). `triage.triage_type` can be `"llm"` or `"tei"` (routes to the same rerank-based classifier instead of an LLM call, via `EmailTriageEngine._rerank`).
5. **Ambiguity escalation**: if Level 1's `confidence_score` is below `triage.confidence_threshold`, the full body is fetched and re-evaluated by the *premium* model (`run_level_1_premium_escalation`) using the same schema — this is a safety net, not a normal path.
6. **Level 2 (premium summarization)**: only for level-2 items. Full body is fetched, sent to `settings.summary_model`, expecting `SummaryResult` JSON (`summary`, `confidence_score`, `tag`).

This exact sequence is duplicated in three places that must stay in sync when changed: `main.py::process_account_emails` (CLI), `mcp_server.py::fetch_and_process_unread::process_emails` (MCP tool), and `auto_rater_runner.py::run_config` (benchmark harness). There is no single shared "run one email through the pipeline" function — the tiering logic lives inline in each caller, with `EmailTriageEngine` only providing the individual stage primitives (`run_level_0_static`, `run_tei_router`, `run_level_1_classification`, `run_level_1_premium_escalation`, `run_level_2_summarization`, `is_vip_sender`).

System prompts for the LLM stages are loaded from `prompts.yml` (keys `level_1_fast_triage`, `level_1_premium_escalation`, `level_2_summarization`) if present, else fall back to hardcoded defaults in `triage.py`.

### Real cache layer (`db.py` — `EmailDB`, SQLite at `<profile>/email_cache.db`)

Every processed message is keyed by RFC `Message-ID` in the `email_cache` table (triage level, tag, reason, score, summary, per-stage token/duration metrics, TEI telemetry). On subsequent runs, `get_cached_result(message_id)` reconstructs the full result dict at zero network/token cost — this is why re-running the CLI repeatedly is cheap. `token_logs` tracks per-call token spend for auditing. New columns are added via best-effort `ALTER TABLE` in `_init_db` (append-only migration pattern — no down-migrations).

### Multi-tenant profiles (`config.py` — `Settings.load_for_profile`)

Everything (DB path, Gmail token/credentials, `.env`) is scoped under `profiles/<name>/`, created on demand. Config resolution order (later overrides earlier): global `data/config.yml` → `profiles/config-local.yml` → `profiles/<name>/config.yml` → `profiles/<name>/config-local.yml`. Within `load_from_yaml`, a YAML value is applied only if the corresponding `EMAIL_TRIAGE_*` env var is *not* already set (env vars always win over YAML). The env file used is `profiles/<name>/.env` if it exists, else the root `.env`. `main.py`, `mcp_server.py`'s `get_resources`, and the auto-rater scripts all call `Settings.load_for_profile(...)` rather than importing the module-level `settings` singleton directly when profile-awareness matters. `data/`, `profiles/`, and `.env` are excluded from the Docker build context via `.dockerignore` and are instead bind-mounted at runtime (see `docker-compose.yml`) so they aren't baked into the image.

The MCP SSE transport additionally supports per-request profile selection via a bearer/`x-profile-token`/`?token=` header mapped through `EMAIL_TRIAGE_PROFILE_TOKEN` entries in each profile's `.env` (see `mcp_server.py::MCPTokenAuthMiddleware` / `load_token_profile_map`).

### Ingestion clients (`gmail_client.py`, `imap_client.py`)

Both expose the same shape: `fetch_unread_messages`/`fetch_unread_headers` (metadata-only, headers `Message-ID/From/Subject/Date` + snippet, never downloads bodies), `fetch_full_body` (only called after a message clears Level 0/0.5/1), `mark_as_read`, `search_messages`, `create_draft`/`create_reply_draft`, `send_reply`. Gmail fetches unread state without marking read by construction (label-based); IMAP explicitly passes `mark_seen=False` everywhere. Gmail metadata fetches use HTTP batching (`_fetch_metadata_batch`, chunks of 100) with a sequential per-message fallback if the batch call fails. IMAP send uses SMTP directly (`smtp_host`/`smtp_port`, falling back to the IMAP login/password if `smtp_login`/`smtp_password` are unset) and appends a copy to the Sent folder afterward.

### CLI output contract (`main.py`)

Default mode emits *only* a raw JSON array to stdout (all logging silenced to `CRITICAL`) so it can be piped into other tooling — never add prints/logs to default-mode paths without gating on `args.human`. `--compact` remaps to short keys (`mid`/`lvl`/`snd`/`sub`/`dt`/`tag`/`sum`) — see `email-triage-engine/references/output_schema.md` for the full field mapping and semantics. `--output <path>` writes the verbose array to disk and prints only a small pointer JSON object to stdout.

### MCP server (`mcp_server.py`)

`RobustFastMCP.call_tool` strips `email_triage__`/`email-triage__` prefixes and fuzzy-matches truncated tool names before dispatch — expect tool names to arrive mangled by some MCP hosts. `get_resources(profile_name)` is the standard entry point for every tool implementation; it re-resolves settings per call (so profile switches take effect without restarting the server).

### Auto Rater suite

Offline benchmarking pipeline independent of the live CLI/MCP paths: `auto_rater_downloader.py` snapshots unread mail to disk, `auto_rater_config.yml` defines named model/routing configurations to sweep, `auto_rater_runner.py` executes the full triage pipeline (its own inline copy, not calling `main.py`) per configuration and writes results per-config, `auto_rater_triage.py` scores classification accuracy against a human-labeled gold set, `auto_rater_summarizer.py` LLM-judges summary quality. `classifier_tester.py` is a separate standalone tool that generates an HTML comparison report across classifier configs on a dataset.

## Configuration reference

- `EMAIL_TRIAGE_*` env vars (see `.env.example`) always take precedence over `data/config.yml`/profile YAML.
- LLM endpoints are decoupled per stage: `EMAIL_TRIAGE_TRIAGE_BASE_URL`/`_API_KEY` for Level 1, `EMAIL_TRIAGE_SUMMARY_BASE_URL`/`_API_KEY` for Level 2 (and premium escalation) — these can point at different proxies/providers.
- Model *names* (`triage_model`, `summary_model`) are set in `data/config.yml` under `llm:`, not in `.env`.
- `email-triage-engine/SKILL.md` documents the CLI's JSON output contract for agent consumers — keep it in sync with any change to `main.py`'s output schema.
