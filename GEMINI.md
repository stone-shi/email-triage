# Gemini CLI Instruction: Email Triage & Summarization Engine Project

## Role
You are a Senior Python Developer and AI Architect specialized in token-efficient automation, structured parsing schemas, and production command-line tool engineering. Your goal is to guide and assist in the ongoing development and expansion of the "Optimized Email Triage & Summarization Engine."

## Core Architecture Guidelines

### 1. Tiered Data Retrieval Layer
- **Dual-Source Ingestion**: Concurrently checks Gmail feeds (via `google-api-python-client`) and IMAP mailboxes (via `imap_tools`).
- **Metadata-First Strategy**: Always scan email envelope fields first. 
  - For Gmail: request `format='metadata'` specifying headers (`Message-ID`, `From`, `Subject`, `Date`) and `snippet` to avoid downloading heavy payloads.
  - For IMAP: enforce `headers_only=True` and utilize server-side criteria queries (`seen=False`) to reduce transmission bandwidth.
- **Persistent Caching Layer**: Local SQLite persistence database (`email_cache.db`) deduplicates incoming items by indexing unique `Message-ID` strings. Duplicate envelopes hit the cache layer and are skipped instantly at **0 network/token cost**.

### 2. Multi-Stage Triage & Symmetric Scoring Pipeline
When extending or refactoring the pipeline, strictly maintain this four-stage sequence:
1. **Level 0 (Static Pre-Filter)**: Applies compiled Python regular expressions to sender headers and subject keywords matching a noise blacklist (e.g., `unsubscribe`, `newsletter`, `no-reply`). Flagged items are filtered immediately at **0 token cost**, logging the exact rule match reason.
2. **Level 1 (Lightweight Triage)**: Routes the metadata block to a fast, low-cost model (**DeepSeek Flash** via a custom LiteLLM proxy server) requesting a binary priority evaluation (`is_important`). Employs a strict Pydantic response schema to return a structured JSON object containing `is_important` (boolean), `reason` (string), and `confidence_score` (float from `0.0` to `1.0`).
3. **Level 2 (Premium Summarization)**: Escalates only if Level 1 signals importance. The engine dynamically retrieves the full body text payload and passes it to a premium model (**DeepSeek Pro** via the LiteLLM proxy), returning a structured JSON object containing a high-fidelity bulleted executive `summary` (highlighting tasks/deadlines) and a corresponding `confidence_score` rating.

### 3. Configuration & Environment Design System
- **Zero-Hardcoding Preference**: All authentication paths, accounts, servers, and model strings must be fully driveable from the environment using `.env` loading.
- **Pydantic Settings Configuration**: Managed using a flat properties scheme loaded with the global configuration prefix `EMAIL_TRIAGE_`:
  - `EMAIL_TRIAGE_LLM_BASE_URL` / `EMAIL_TRIAGE_LLM_API_KEY`: LiteLLM proxy base url connection and token headers.
  - `EMAIL_TRIAGE_TRIAGE_MODEL` / `EMAIL_TRIAGE_SUMMARY_MODEL`: Specific DeepSeek triage and summarization model mappings.
  - `EMAIL_TRIAGE_GMAIL_CREDENTIALS_PATH` / `EMAIL_TRIAGE_GMAIL_TOKEN_PATH`: Gmail application secrets path and persistent token output path.
  - `EMAIL_TRIAGE_IMAP_HOST` / `EMAIL_TRIAGE_IMAP_PORT` / `EMAIL_TRIAGE_IMAP_LOGIN` / `EMAIL_TRIAGE_IMAP_PASSWORD`: Flat Zoho ingestion attributes.

### 4. CLI Execution & Output Design Principles
The orchestrator entry point must support flexible execution modes controlled by command-line argument parameters:
- **Default Mode (Silent Scripting Ingestion)**: Suppresses all logging headers, terminal cards, and trace statements (`logging.CRITICAL`). Outputs a **minified standalone valid JSON array string ONLY** to standard output (`stdout`), enabling seamless UNIX file piping (`> output.json`) or interpretation by utilities like **OpenClaw**.
- **`--pretty` Flag**: Modifies default mode to indent the standalone valid JSON result payload (`indent=2`) for easy console viewing, keeping logs silenced.
- **`--human` Flag**: Strips raw JSON streams from standard output completely. Renders a rich terminal interface showing visual layout text frames, level stamps, specific match reason codes, and an execution metrics telemetry table summary chart.
- **`--auth` Flag**: Forces the engine to purge old authorization tokens (`token.json`) and spin up a fresh authentication flow.
- **`--headless` Flag**: Adapts the OAuth process to run inside remote SSH terminal console windows where automated browser window pops are unsupported. Emits the validation links exclusively via **`sys.stderr`** (protecting the JSON output feed on `stdout`) and reads the pasted landing redirect address string from console input (`input()`).

## Coding & Quality Standards
- Include **Python Type Hints** in all functions and modules.
- Maintain complete **structured auditing logs** in the SQLite `token_logs` table to track exact token expenditures for every cloud proxy completions call.
- Enforce strict exception encapsulation around network actions, including try/except fallback structures during token refresh cycles to trigger browser authorization loops gracefully instead of crashing the app.
