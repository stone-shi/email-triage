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

This exact sequence is duplicated in two places that must stay in sync when changed: `main.py::process_account_emails` (CLI) and `mcp_server.py::_run_tiered_triage` (the MCP server's background sync engine — see below), plus `auto_rater_runner.py::run_config` (benchmark harness). There is no single shared "run one email through the pipeline" function — the tiering logic is duplicated in each of these, with `EmailTriageEngine` only providing the individual stage primitives (`run_level_0_static`, `run_tei_router`, `run_level_1_classification`, `run_level_1_premium_escalation`, `run_level_2_summarization`, `is_vip_sender`). Unlike `main.py`'s version, `mcp_server.py::_run_tiered_triage` always receives an already-downloaded `full_body` rather than lazily fetching it mid-pipeline, since the MCP server's sync engine downloads full content for every unread message upfront (see "Background sync scheduler" below).

System prompts for the LLM stages are loaded from `prompts.yml` (keys `level_1_fast_triage`, `level_1_premium_escalation`, `level_2_summarization`) if present, else fall back to hardcoded defaults in `triage.py`.

### Real cache layer (`db.py` — `EmailDB`, SQLite at `<profile>/email_cache.db`)

Every processed message is keyed by RFC `Message-ID` in the `email_cache` table (triage level, tag, reason, score, summary, per-stage token/duration metrics, TEI telemetry, plus download-tracking columns `snippet`/`is_unread`/`source_id`/`downloaded_at`/`display_count` — the last incremented by `fetch_and_process_unread` each time a triaged row is actually rendered, and read back by auto-mark-read). On subsequent runs, `get_cached_result(message_id)` reconstructs the full result dict at zero network/token cost — this is why re-running the CLI repeatedly is cheap. `token_logs` tracks per-call token spend for auditing. New columns are added via best-effort `ALTER TABLE` in `_init_db` (append-only migration pattern — no down-migrations).

A row can now exist in `email_cache` *before* triage has run: `upsert_email_metadata(...)` is the download-phase write (populates `email_body`/`snippet`/`is_unread`/`source_id` without touching triage columns), while `save_triage_result(...)` is the triage-phase write. Both use `INSERT ... ON CONFLICT(message_id) DO UPDATE` rather than a destructive replace, with `email_body` specifically protected via `COALESCE(excluded.email_body, email_cache.email_body)` so a triage-phase write that passes `email_body=None` (e.g. the Level-0/TEI-noise branches, which never need a body) doesn't null out content the download phase already fetched. Because of this, "row exists" no longer means "already triaged" — `is_processed(message_id)` and any cache-skip check must test `triage_level IS NOT NULL`, not just row presence.

`sync_state` (`account TEXT PRIMARY KEY, checkpoint_val TEXT`) is used as a generic per-account JSON blob store via `save_sync_summary`/`get_sync_summary`, holding each account's last-download timestamp/counts/errors for the `get_last_download_time` MCP tool.

### Multi-tenant profiles (`config.py` — `Settings.load_for_profile`)

Everything (DB path, Gmail token/credentials, `.env`) is scoped under `profiles/<name>/`, created on demand. Config resolution order (later overrides earlier): global `data/config.yml` → `profiles/config-local.yml` → `profiles/<name>/config.yml` → `profiles/<name>/config-local.yml`. Within `load_from_yaml`, a YAML value is applied only if the corresponding `EMAIL_TRIAGE_*` env var is *not* already set (env vars always win over YAML). The env file used is `profiles/<name>/.env` if it exists, else the root `.env`. `main.py`, `mcp_server.py`'s `get_resources`, and the auto-rater scripts all call `Settings.load_for_profile(...)` rather than importing the module-level `settings` singleton directly when profile-awareness matters. `data/`, `profiles/`, and `.env` are excluded from the Docker build context via `.dockerignore` and are instead bind-mounted at runtime (see `docker-compose.yml`) so they aren't baked into the image.

The MCP SSE transport additionally supports per-request profile selection via a bearer/`x-profile-token`/`?token=` header mapped through `EMAIL_TRIAGE_PROFILE_TOKEN` entries in each profile's `.env` (see `mcp_server.py::MCPTokenAuthMiddleware` / `load_token_profile_map`). `config.py::list_profile_names()` is the single shared helper for enumerating "all configured accounts" (every subdirectory under `profiles/`, always including `default`) — used by `load_token_profile_map` and by the background sync scheduler's `sync_all_profiles()`.

### Ingestion clients (`gmail_client.py`, `imap_client.py`)

Both expose the same shape: `fetch_unread_messages`/`fetch_unread_headers` (metadata-only, headers `Message-ID/From/Subject/Date` + snippet, never downloads bodies), `fetch_full_body`, `mark_as_read`, `search_messages`, `create_draft`/`create_reply_draft`, `send_reply`. In the CLI pipeline (`main.py`), `fetch_full_body` is only called after a message clears Level 0/0.5/1; the MCP server's background sync engine (`mcp_server.py::sync_account`) instead fetches full body upfront for every unread message (skipping the fetch if already cached), since it always persists content regardless of triage outcome. Gmail fetches unread state without marking read by construction (label-based); IMAP explicitly passes `mark_seen=False` everywhere. Gmail metadata fetches use HTTP batching (`_fetch_metadata_batch`, chunks of 100) with a sequential per-message fallback if the batch call fails; `fetch_full_bodies_batch` batches the same way. Both batch methods route every chunk through `GmailClient._throttle_batch_call`, which sleeps as needed to keep at least `_INTER_BATCH_DELAY_SECONDS` (2s) between successive HTTP batch calls **on that client instance**, including across separate top-level calls (not just between chunks within one call) — Gmail counts every sub-request in a batch against the per-user-per-minute quota individually, so without this a large job (e.g. the full-mailbox downloader below) blows through quota and gets `403 rateLimitExceeded` within the first minute; the existing exponential-backoff retry in both methods is a reactive fallback for whatever still slips through, not a substitute for this proactive pacing. IMAP send uses SMTP directly (`smtp_host`/`smtp_port`, falling back to the IMAP login/password if `smtp_login`/`smtp_password` are unset) and appends a copy to the Sent folder afterward.

#### Full mailbox archive downloader (manual or nightly, no triage)

Step 1 of a planned local full-archive + embedding search feature (not built yet): a "Download All" button per profile on the dashboard (`POST /api/download_all/start`) triggers `mcp_server.py::full_download_profile` → `full_download_account` for Gmail and/or IMAP. Unlike the regular sync engine, this lists **every** message in the mailbox — `GmailClient.list_all_ids()` (no `q` filter, matching Gmail's own "All Mail" scope: everything except Spam/Trash) / `IMAPClient.fetch_all_headers()` (`AND(all=True)`, same INBOX-only scope as the rest of this client) — and persists metadata + body via the same download-phase `upsert_email_metadata` used elsewhere, but **never calls the triage pipeline**, so `triage_level` stays `NULL` for everything it downloads. It's resumable: `db.get_archived_source_ids(account)` (source_ids with a cached `email_body`) lets it skip messages already fully archived on a prior run, so re-clicking after an interruption (or a quota-related partial failure) just continues. It shares the regular sync's per-profile `threading.Lock`/stop-event (`_get_profile_lock`/`_get_stop_event`), so it can't run concurrently with a normal sync for the same profile, and the existing Stop button cancels it too.

Also runs on its own recurring schedule (MCP server, SSE transport only), independent of the regular sync's `scheduler:` block: `download_all_scheduler_loop` in `mcp_server.py`'s `__main__`, gated on `settings.download_all_scheduler.enabled` (default `true`) and firing every `settings.download_all_scheduler.interval_seconds` (default `"24h"` — nightly). Unlike `scheduler_loop`, it *sleeps first* rather than firing immediately on startup, since a full mailbox listing on every container restart would be wasteful; after the first (expensive) backfill, subsequent runs are cheap since already-archived messages are skipped, so nightly just picks up whatever's new. This is process-wide like `scheduler:`, not resolved per-profile.

### CLI output contract (`main.py`)

Default mode emits *only* a raw JSON array to stdout (all logging silenced to `CRITICAL`) so it can be piped into other tooling — never add prints/logs to default-mode paths without gating on `args.human`. `--compact` remaps to short keys (`mid`/`lvl`/`snd`/`sub`/`dt`/`tag`/`sum`) — see `email-triage-engine/references/output_schema.md` for the full field mapping and semantics. `--output <path>` writes the verbose array to disk and prints only a small pointer JSON object to stdout.

### MCP server (`mcp_server.py`)

`RobustFastMCP.call_tool` strips `email_triage__`/`email-triage__` prefixes and fuzzy-matches truncated tool names before dispatch — expect tool names to arrive mangled by some MCP hosts. `get_resources(profile_name)` is the standard entry point for every tool implementation; it re-resolves settings per call (so profile switches take effect without restarting the server).

`fetch_and_process_unread` is **cache-only**: it never calls Gmail/IMAP, it just reads `db.get_unread_emails(account=...)` and renders whatever's currently cached as unread (including a "pending background triage" section for rows that have been downloaded but not yet classified). All live downloading + triaging now happens out-of-band via the background sync engine described below; use `trigger_download` to force a refresh and `get_last_download_time` to check staleness before trusting `fetch_and_process_unread`'s output. `search_emails` is unaffected and still does a live Gmail/IMAP search.

#### Background sync scheduler

Under SSE transport (the only long-running mode — see Docker below), `mcp_server.py`'s `__main__` block runs an `anyio` task group alongside the uvicorn server: a `scheduler_loop` fires `sync_all_profiles()` immediately on startup, then every `settings.scheduler.interval_seconds` (default 15 minutes, gated on `settings.scheduler.enabled`). Under stdio transport the scheduler is a no-op (logged) since `FastMCP.run()` blocks with no task-injection point.

- `sync_all_profiles()` → `sync_profile(name)` for every `list_profile_names()` entry → `sync_account(...)` once each for the profile's Gmail and IMAP client. `sync_profile` skips a side entirely (no client construction, no network call) if its identity is still the uninitialized placeholder default (`_PLACEHOLDER_GMAIL_ACCOUNT`/`_PLACEHOLDER_IMAP_LOGIN`) — this matters because `list_profile_names()` always includes `"default"`, so a `profiles/default/` directory that was never actually configured (no `.env`/credentials) would otherwise fail loudly with auth errors on every tick.
- `sync_account` lists the *complete* live-unread set (unbounded, not capped by `max_per_account`) and reconciles it against `db.get_unread_message_ids(account)` (anything cached-unread but no longer live-unread gets flipped to read via `upsert_email_metadata(..., is_unread=False)` — this is the "did someone read it elsewhere" check). For Gmail specifically, listing goes through `GmailClient.list_unread_ids` (bare `messages.list` pagination, no per-message metadata) plus `mcp_server.py::_resolve_gmail_live_metadata`, which reuses `db.get_known_source_metadata` for internal ids already seen on a prior tick and only calls `_fetch_metadata_batch` for genuinely new ones — otherwise an account with a large persistent backlog would re-fetch metadata for its entire unread set (thousands of messages) every tick and trip Gmail's per-user rate limit. IMAP still uses `fetch_unread_headers` directly (no equivalent metadata cache, since IMAP header fetches aren't rate-limited the same way). From the reconciled live set, up to `scheduler.max_per_account` messages are selected for download+triage, *prioritizing not-yet-triaged messages* (via `db.get_triaged_message_ids`) over whatever the live list's natural (recency) ordering would pick — otherwise a backlog larger than `max_per_account` would have its tail permanently starved, since the same top-N slice would be reselected every tick. Selected messages get full body content downloaded and `_run_tiered_triage` run if not already triaged.
- `sync_profile` is guarded by a per-profile `threading.Lock` (non-blocking `acquire`) shared between the scheduler tick and a manual `trigger_download` call, so the same profile is never synced concurrently — a racing call just gets `{"status": "skipped"}`.
- Gmail's `list_unread_ids` follows `nextPageToken` to return the full unread set (fixed as part of this feature — it used to silently cap at Gmail's default 100-message page); IMAP's `fetch_unread_headers` was already unbounded via `imap_tools`.
- **Auto-mark-read** (opt-in, default off; configured independently per triage level via `auto_mark_read.level_0`/`level_1`/`level_2`, each its own `enabled`/`after_displays`): the last step of `sync_account` builds a `{triage_level: after_displays}` dict from whichever levels have `enabled=True`, then marks matching messages read — both remotely (`client.mark_as_read`) and in the local cache — once a message has been triaged into that level *and* shown to the user via `fetch_and_process_unread` at least that level's `after_displays` times. A level omitted from the dict (i.e. not enabled) is never auto-marked regardless of display count, so e.g. level 0 (noise) can be auto-cleared after one view while level 2 (important) stays fully manual. `fetch_and_process_unread` bumps a per-message `email_cache.display_count` column (`db.increment_display_count`) for every triaged row it actually renders; `db.get_auto_mark_read_candidates(account, thresholds)` is what `sync_account` reads back to find eligible messages per level. Since this only runs from the sync engine (which already holds a live Gmail/IMAP client per tick), the cache-only `fetch_and_process_unread` tool itself never touches the network.

### Auto Rater suite

Offline benchmarking pipeline independent of the live CLI/MCP paths: `auto_rater_downloader.py` snapshots unread mail to disk, `auto_rater_config.yml` defines named model/routing configurations to sweep, `auto_rater_runner.py` executes the full triage pipeline (its own inline copy, not calling `main.py`) per configuration and writes results per-config, `auto_rater_triage.py` scores classification accuracy against a human-labeled gold set, `auto_rater_summarizer.py` LLM-judges summary quality. `classifier_tester.py` is a separate standalone tool that generates an HTML comparison report across classifier configs on a dataset.

## Configuration reference

- `EMAIL_TRIAGE_*` env vars (see `.env.example`) always take precedence over `data/config.yml`/profile YAML.
- LLM endpoints are decoupled per stage: `EMAIL_TRIAGE_TRIAGE_BASE_URL`/`_API_KEY` for Level 1, `EMAIL_TRIAGE_SUMMARY_BASE_URL`/`_API_KEY` for Level 2 (and premium escalation) — these can point at different proxies/providers.
- Model *names* (`triage_model`, `summary_model`) are set in `data/config.yml` under `llm:`, not in `.env`.
- Background sync scheduler (MCP server, SSE transport only): `scheduler:` block in `data/config.yml` (`enabled`, `interval` — a duration string like `"15m"`/`"1h"` parsed by `config.py::parse_duration`, `max_per_account`, `days`), or `EMAIL_TRIAGE_SCHEDULER_ENABLED`/`_INTERVAL`/`_MAX_PER_ACCOUNT`/`_DAYS` env vars. This is process-wide (one scheduler loop syncs every profile per tick), so it's read from the global/default `settings` singleton, not resolved per-profile.
- Full-mailbox download scheduler (MCP server, SSE transport only): `download_all_scheduler:` block in `data/config.yml` (`enabled`, default `true`; `interval`, default `"24h"`), or `EMAIL_TRIAGE_DOWNLOAD_ALL_SCHEDULER_ENABLED`/`_INTERVAL` env vars. Also process-wide, independent of `scheduler:`.
- Auto-mark-read (opt-in, default off): `auto_mark_read:` block in `data/config.yml`, with `level_0`/`level_1`/`level_2` each independently configured (`enabled`, `after_displays` — how many times `fetch_and_process_unread` must have shown a message triaged into that level before it's auto-marked read), or `EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_{0,1,2}_ENABLED`/`_AFTER_DISPLAYS` env vars. Unlike `scheduler:`, this is resolved per-profile (read from `settings_instance` inside `sync_account`), so different profiles — and different levels within a profile — can enable it independently. See "Auto-mark-read" under Background sync scheduler above.
- `email-triage-engine/SKILL.md` documents the CLI's JSON output contract for agent consumers — keep it in sync with any change to `main.py`'s output schema.
