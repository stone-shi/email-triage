# Optimized Email Triage & Summarization Engine

A token-efficient, multi-stage email ingestion and triage pipeline engineered in Python. The system processes multiple accounts concurrently (Gmail API & Zoho IMAP), filters noise instantly, applies lightweight LLM classification, and generates premium bulleted summaries for critical alerts.

---

## 📐 Core Architecture Levels

The engine leverages a multi-stage tiered triage architecture to eliminate redundant processing and minimize token expenditure:

1. **Level 0 (Static Pre-Filter)**: Connects incoming headers against compiled keyword and sender blacklists (e.g., `no-reply`, `digest`). Intercepts noise immediately at **0 token cost** and logs exact match reasons.
2. **Level 1 (Lightweight Triage)**: Packages the metadata envelope (From, Subject, Snippet) and requests an ultra-fast binary assessment (`is_important`) from **DeepSeek Flash** (`deepseek/deepseek-v4-flash`) using a strict Pydantic JSON response schema, returning a reason and confidence score.
3. **Level 2 (Premium Summarization)**: If Level 1 signals high priority, the engine escalates to download the full message payload and invokes the premium **DeepSeek Pro** model (`deepseek/deepseek-v4-pro`) to render high-fidelity summaries, deadlines, and action items.

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
Create a `.env` file in the root directory to manage runtime configurations. All options are driveable from the environment without code tweaks:
```ini
# LiteLLM Proxy Proxy Configuration
EMAIL_TRIAGE_LLM_BASE_URL=https://your-llm-proxy.com/v1
EMAIL_TRIAGE_LLM_API_KEY=your_api_key_here
EMAIL_TRIAGE_TRIAGE_MODEL=deepseek/deepseek-v4-flash
EMAIL_TRIAGE_SUMMARY_MODEL=deepseek/deepseek-v4-pro

# Gmail OAuth Parameters and File Paths
EMAIL_TRIAGE_GMAIL_CREDENTIALS_PATH=credentials.json
EMAIL_TRIAGE_GMAIL_TOKEN_PATH=token.json
EMAIL_TRIAGE_GMAIL_ACCOUNT=your_email@gmail.com

# Zoho IMAP Ingestion Parameters
EMAIL_TRIAGE_IMAP_HOST=imap.zoho.com
EMAIL_TRIAGE_IMAP_PORT=993
EMAIL_TRIAGE_IMAP_LOGIN=your_email@domain.com
EMAIL_TRIAGE_IMAP_PASSWORD=your_app_password_here
```

---

## 🚀 Usage & Execution Modes

The engine can be easily invoked using the `./triage.sh` wrapper script, which automatically handles virtual environment activation. The orchestrator supports multiple distinct output formatting modes tailored for terminal reading or downstream tool ingestion (e.g., **OpenClaw**):

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
Completely strips raw JSON dumps and outputs a premium visual terminal interface. Displays text cards, specific triage level stamps, metrics logs, and telemetry summary charts:
```bash
./triage.sh --human
```

### 4. Forced Re-Authentication (`--auth`)
Forces the application to purge its current persistent authorization credentials (`token.json`) and re-open the interactive browser callback process. Can be combined with any formatting mode:
```bash
./triage.sh --auth --human
```

### 5. Headless Console Mode (`--headless`)
Enables full interactive OAuth re-authorization inside remote server terminals or SSH sessions where automatic browser window loading is unsupported. Prints the required link and reads the pasted redirect URL landing address string from console input:
```bash
./triage.sh --auth --headless
```

---

## 🗄 Local Storage & Auditing
- **`email_cache.db`**: Local SQLite database containing active `Message-ID` hashes to ensure duplicate emails are skipped instantly on subsequent scans, alongside token log counters auditing proxy usage histories.
