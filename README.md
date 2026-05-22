# Optimized Email Triage & Summarization Engine

A token-efficient, multi-stage email ingestion and triage pipeline engineered in Python. The system processes multiple accounts concurrently (Gmail API & Zoho IMAP), filters noise instantly, applies lightweight LLM classification, and generates premium bulleted summaries for critical alerts.

---

## 📐 Core Architecture Levels

The engine leverages a multi-stage tiered triage architecture to eliminate redundant processing and minimize token expenditure:

1. **Level 0 (Static Pre-Filter)**: Connects incoming headers against compiled keyword and sender blacklists (e.g., `no-reply`, `digest`). Intercepts noise immediately at **0 token cost**, assigns the `"low"` classification tag, and logs exact match reasons.
2. **Level 1 (Lightweight Triage)**: Packages the metadata envelope (From, Subject, Snippet) and requests an ultra-fast binary assessment (`is_important`) alongside a granular one-word lowercase classification `"tag"` (e.g., `promotion`, `notification`, `personal`, `vip`) from **DeepSeek Flash** (`deepseek/deepseek-v4-flash`) using a strict Pydantic JSON response schema.
3. **Level 2 (Premium Summarization)**: If Level 1 signals high priority, the engine escalates to download the full message payload and invokes the premium **DeepSeek Pro** model (`deepseek/deepseek-v4-pro`) to render high-fidelity summaries, deadlines, action items, and context-aware tags.

---

## 🛠 Setup & Installation

### 1. Environment Initialization
The engine requires Python 3.13+ and is self-contained inside a local virtual environment:
```bash
# Create virtual environment
python3 -m venv venv

# Install dependencies via PyPI simple index
./venv/bin/pip install -r requirements.txt --index-url https://pypi.org/simple
```

### 2. Local Configuration (`.env`)
Create a `.env` file in the root directory to manage runtime configurations. All options are driveable from the environment without code tweaks.

Note that LLM model definitions (i.e. which models to use) reside inside `config.yml`. The `.env` file specifies provider base URLs and API keys, allowing you to use different endpoints/providers for Triage (Level 1) and Summarization (Level 2):

```ini
# Decoupled LLM Provider Configurations
EMAIL_TRIAGE_TRIAGE_BASE_URL=https://your-llm-proxy.com/v1
EMAIL_TRIAGE_TRIAGE_API_KEY=your_api_key_here
EMAIL_TRIAGE_SUMMARY_BASE_URL=https://your-llm-proxy.com/v1
EMAIL_TRIAGE_SUMMARY_API_KEY=your_api_key_here

# TEI Sequence Classifier URL
EMAIL_TRIAGE_TEI_URL=http://10.100.0.50:8077/predict

# Gmail OAuth Parameters and File Paths
EMAIL_TRIAGE_GMAIL_CREDENTIALS_PATH=credentials.json
EMAIL_TRIAGE_GMAIL_TOKEN_PATH=token.json
EMAIL_TRIAGE_GMAIL_ACCOUNT=your_email@gmail.com

# Zoho IMAP Ingestion Parameters
EMAIL_TRIAGE_IMAP_HOST=imap.zoho.com
EMAIL_TRIAGE_IMAP_PORT=993
EMAIL_TRIAGE_IMAP_LOGIN=your_email@domain.com
EMAIL_TRIAGE_IMAP_PASSWORD=your_app_password_here

# MCP Server Settings
EMAIL_TRIAGE_MCP_TRANSPORT=stdio
EMAIL_TRIAGE_MCP_HOST=0.0.0.0
EMAIL_TRIAGE_MCP_PORT=8000
```

---

## 🚀 Usage & Execution Modes

The engine supports highly robust stateless unread metadata scanning. The persistent SQLite database acts as a **Real Cache Layer**: incoming unread emails that already exist in the cache are instantly retrieved, reconstructed into fully uniform dictionaries, and displayed/appended at **0 network/token cost**. IMAP fetching explicitly preserves unread states via `mark_seen=False`.

The orchestrator entrypoint (`main.py` / `triage.sh`) supports multiple runtime arguments:

### 1. Standard Scripting Mode (Default)
Outputs a **minified, raw single-line valid JSON array** representing all triaged items. Completely silences internal logs and text formatting to allow clean file-piping or automation interpretation:
```bash
./triage.sh
```

### 2. Pretty JSON Mode (`--pretty`)
Emits the standalone valid JSON result payload formatted with clean indentation for easy inspection, keeping logs suppressed:
```bash
./triage.sh --pretty
```

### 3. Human Interactive Mode (`--human`)
Completely strips raw JSON dumps and outputs a premium visual terminal interface. Displays text cards, specific triage level stamps, classification tags, metrics logs, and telemetry summary charts:
```bash
./triage.sh --human
```

### 4. Ingestion Slice Limit (`--max <n>`)
Restricts processing to only the top `n` unread emails retrieved from each configured ingestion source (Gmail + IMAP total capped at `2n`):
```bash
./triage.sh --pretty --max 5
```

### 5. Date Cutoff Filter (`--days <n>`)
Parses incoming RFC/ISO envelope dates and filters processing strictly to unread emails received within the last `N` days:
```bash
./triage.sh --human --days 3
```

### 6. LLM Agent Optimization Arguments
Designed specifically to minimize context window token consumption when called programmatically by autonomous agents:
- **`--level <n>`**: Filters output to emit only JSON objects matching a specific triage level threshold or higher (e.g., `--level 2` extracts only critical actionable briefs).
- **`--compact`**: Emits a minified JSON schema (`mid`, `lvl`, `snd`, `sub`, `dt`, `tag`, `sum`) dropping verbose justification reason strings to save context window tokens.
- **`--skip <n>` / `--limit <n>`**: Enforces strict pagination offset slicing to process huge backlogs incrementally.
- **`--output <path>`**: Writes the full verbose JSON array directly to disk while emitting only a lightweight pointer summary to `stdout` (e.g., `{"status": "success", "total_returned": 120, "file_uri": "..."}`).

### 7. Authentication & Headless Flows
- **`--auth`**: Purges active credentials (`token.json`) and re-opens the authorization loop.
- **`--headless`**: Emits OAuth confirmation links exclusively via `sys.stderr` for headless SSH environments.

---

## 🤖 Model Context Protocol (MCP) Server

The engine includes a complete Model Context Protocol (MCP) server implementation (`mcp_server.py`) powered by `FastMCP`. This exposes database queries, full text search, and live triage tools directly to AI editors, hosts, and remote orchestrators.

### Exposing Production MCP Tools:
| Tool Name | Parameters | Description |
|---|---|---|
| `fetch_and_process_unread` | `max_per_source` (int), `days` (int) | Triggers unread email ingestion from Gmail and IMAP, executes multi-tier triage, and caches results. |
| `list_cached_emails` | `limit` (int), `triage_level` (int) | Retrieves metadata for recently processed email records from SQLite. |
| `get_email_details` | `message_id` (str) | Fetches the full detailed record of a cached email, including pipeline reason, confidence score, and premium summaries. |
| `triage_single_email` | `message_id` (str) | Forces a full re-evaluation/triage of a specific cached email. |
| `search_emails` | `query` (str) | Performs text search across sender, subject, and email body fields in SQLite. |

---

### Run Modes & Transports

#### Mode A: Stdio Transport (Default)
Ideal for standard IDE editors (e.g. Claude Desktop) or orchestration agents managing local processes. The server communicates directly via `stdin`/`stdout`.

**Running locally in Docker:**
```bash
docker run -i --rm \
  --env-file .env \
  -v "$(pwd)/email_cache.db:/app/email_cache.db" \
  -v "$(pwd)/token.json:/app/token.json" \
  email-triage-email-triage
```

**Testing stdio communication manually:**
```bash
echo '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}' | \
docker run -i --rm --env-file .env email-triage-email-triage
```

---

#### Mode B: HTTP Server-Sent Events (SSE) Transport
Ideal for containers, web applications, or remote orchestrators. The server starts an HTTP application running on Starlette & Uvicorn.

**Running locally in Docker:**
```bash
docker run -d --name email_triage_sse \
  -p 8000:8000 \
  -e EMAIL_TRIAGE_MCP_TRANSPORT=sse \
  --env-file .env \
  -v "$(pwd)/email_cache.db:/app/email_cache.db" \
  -v "$(pwd)/token.json:/app/token.json" \
  email-triage-email-triage
```

**Verifying the HTTP/SSE stream:**
```bash
curl -i http://localhost:8000/sse
```

---

## 📊 Auto Rater: Testing & Benchmarking Suite

The system incorporates a high-fidelity automated testing suite called **Auto Rater** to execute side-by-side model benchmarking, track tag distributions, and evaluate classification accuracy without affecting production data.

### Execution Workflow Tiers

1. **Batch Ingestion Downloader**: Pulls unread messages offline into plain text body schemas:
   ```bash
   ./venv/bin/python3 auto_rater_downloader.py
   ```
2. **Isolated Benchmark Runner**: Loops over configurations specified inside `auto_rater_config.yml`, executing all triage steps with token/tag tracking:
   ```bash
   ./venv/bin/python3 auto_rater_runner.py
   ```
3. **Triage Classifier Rater**: Calculates precision, recall, relative accuracy, balanced F1 scores, and **Tag Matching Alignment Accuracy** relative to human gold standard baselines:
   ```bash
   ./venv/bin/python3 auto_rater_triage.py
   ```
4. **LLM-as-a-Judge Summary Rater**: Uses a premium judge model to score executive summaries on a strict 1-10 scale:
   ```bash
   ./venv/bin/python3 auto_rater_summarizer.py
   ```
5. **Missing Tag Backfill Utility [NEW]**: Evaluates and backfills granular classification tags for older existing profile benchmarking datasets:
   ```bash
   ./venv/bin/python3 add_missing_tags.py --profile baseline_gemini_pro
   ```

---

## 🗄 Local Storage & Auditing
- **`email_cache.db`**: Local SQLite database containing active `Message-ID` hashes, real cache full row storage, classification tags, and auditing token logs.
