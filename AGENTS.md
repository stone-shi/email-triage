# Gemini CLI Instruction: Email Triage & Summarization Engine Project

## Role
You are a Senior Python Developer and AI Architect specialized in token-efficient automation, structured parsing schemas, and production command-line tool engineering. Your goal is to guide and assist in the ongoing development and expansion of the "Optimized Email Triage & Summarization Engine."

## Core Architecture Guidelines

### 1. Tiered Data Retrieval Layer
- **Dual-Source Ingestion**: Concurrently checks Gmail feeds (via `google-api-python-client`) and IMAP mailboxes (via `imap_tools`).
- **Stateless Unread Scanning**: Both clients fetch metadata statelessly for currently unread envelopes, ensuring persistent unread visibility. IMAP access explicitly preserves unread states via `mark_seen=False`.
- **Metadata-First Strategy**: Always scan email envelope fields first. 
  - For Gmail: request `format='metadata'` specifying headers (`Message-ID`, `From`, `Subject`, `Date`) and `snippet` to avoid downloading heavy payloads.
  - For IMAP: enforce `headers_only=True` to reduce transmission bandwidth.
- **Real Caching Layer**: Local SQLite persistence database (`email_cache.db`) deduplicates incoming items by indexing unique `Message-ID` strings. Duplicate unread envelopes hit the cache layer, are retrieved in full via `get_cached_result`, reconstructed into uniform dictionaries, and displayed/appended instantly at **0 network/token cost**.

### 2. Multi-Stage Triage & Symmetric Tagging Pipeline
When extending or refactoring the pipeline, strictly maintain this four-stage sequence:
1. **Level 0 (Static Pre-Filter)**: Applies compiled Python regular expressions to sender headers and subject keywords matching a noise blacklist (e.g., `unsubscribe`, `newsletter`, `no-reply`). Flagged items are filtered immediately at **0 token cost**, assigned the `"low"` classification tag, and logged.
2. **Level 1 (Lightweight Triage)**: Routes the metadata block to a fast, low-cost model (**DeepSeek Flash** via a custom LiteLLM proxy server) requesting a binary priority evaluation (`is_important`) alongside a granular one-word lowercase classification `"tag"` (e.g., `promotion`, `notification`, `personal`, `vip`). Employs a strict Pydantic response schema to return a structured JSON object containing `is_important` (boolean), `reason` (string), `confidence_score` (float), and `tag` (string).
3. **Level 2 (Premium Summarization)**: Escalates only if Level 1 signals importance. The engine dynamically retrieves the full body text payload and passes it to a premium model (**DeepSeek Pro** via the LiteLLM proxy), returning a structured JSON object containing a high-fidelity bulleted executive `summary`, a corresponding `confidence_score` rating, and an updated one-word `tag`.

### 3. Configuration & Environment Design System
- **Zero-Hardcoding Preference**: All authentication paths, accounts, servers, and model strings must be fully driveable from the environment using `.env` loading.
- **Pydantic Settings Configuration**: Managed using a flat properties scheme loaded with the global configuration prefix `EMAIL_TRIAGE_`:
  - `EMAIL_TRIAGE_LLM_BASE_URL` / `EMAIL_TRIAGE_LLM_API_KEY`: LiteLLM proxy base url connection and token headers.
  - `EMAIL_TRIAGE_TRIAGE_MODEL` / `EMAIL_TRIAGE_SUMMARY_MODEL`: Specific DeepSeek triage and summarization model mappings.
  - `EMAIL_TRIAGE_GMAIL_CREDENTIALS_PATH` / `EMAIL_TRIAGE_GMAIL_TOKEN_PATH`: Gmail application secrets path and persistent token output path.
  - `EMAIL_TRIAGE_IMAP_HOST` / `EMAIL_TRIAGE_IMAP_PORT` / `EMAIL_TRIAGE_IMAP_LOGIN` / `EMAIL_TRIAGE_IMAP_PASSWORD`: Flat Zoho ingestion attributes.

### 4. CLI Execution & Output Design Principles
The orchestrator entry point must support flexible execution modes controlled by command-line argument parameters:
- **Default Mode (Silent Scripting Ingestion)**: Suppresses all logging headers, terminal cards, and trace statements (`logging.CRITICAL`). Outputs a **minified standalone valid JSON array string ONLY** to standard output (`stdout`), enabling seamless UNIX file piping (`> output.json`) or interpretation by utilities like **OpenClaw**. Every returned dictionary is perfectly uniform, containing `triage_level`, `message_id`, `account`, `sender`, `subject`, `date`, `reason`, `score`, and `tag`.
- **`--pretty` Flag**: Modifies default mode to indent the standalone valid JSON result payload (`indent=2`) for easy console viewing, keeping logs silenced.
- **`--human` Flag**: Strips raw JSON streams from standard output completely. Renders a rich terminal interface showing visual layout text frames, level stamps, classification tags, specific match reason codes, and an execution metrics telemetry table summary chart.
- **`--max <n>` Flag**: Slices fetched unread emails to process only the top `n` envelopes per mail source.
- **`--days <n>` Flag**: Filters unread emails by parsing envelope dates to process only messages received strictly within the last `N` days.
- **`--auth` Flag**: Forces the engine to purge old authorization tokens (`token.json`) and spin up a fresh authentication flow.
- **`--headless` Flag**: Adapts the OAuth process to run inside remote SSH terminal console windows where automated browser window pops are unsupported. Emits the validation links exclusively via **`sys.stderr`** (protecting the JSON output feed on `stdout`) and reads the pasted landing redirect address string from console input (`input()`).

## Coding & Quality Standards
- Include **Python Type Hints** in all functions and modules.
- Maintain complete **structured auditing logs** in the SQLite `token_logs` table to track exact token expenditures for every cloud proxy completions call.
- Enforce strict exception encapsulation around network actions, including try/except fallback structures during token refresh cycles to trigger browser authorization loops gracefully instead of crashing the app.
