# Project: Optimized Email Triage & Summarization Engine

## Objective
To replace the current high-token usage email workflow with a tiered Python-based pipeline that utilizes local caching, metadata-first filtering, and local LLM triage.

## Phase 1: Authentication & Infrastructure
- [ ] **Google Cloud Setup**: Create a project in the Google Cloud Console, enable Gmail API, and download `credentials.json`.
- [ ] **OAuth Implementation**: Write a Python script using `google-auth-oauthlib` to generate and persist `token.json` (Refresh Token).
- [ ] **IMAP Configuration**: Configure `imap_tools` credentials for non-Gmail accounts (Himalaya alternative).
- [ ] **Database Setup**: Initialize a local Redis instance or SQLite database (`email_cache.db`) to track `Message-ID` and classification status.

## Phase 2: Data Retrieval Layer
- [ ] **Gmail Metadata Fetcher**: Implement a client that calls `service.users().messages().list(q="is:unread")` and then fetches `format='metadata'` for each ID.
- [ ] **IMAP Header Fetcher**: Implement a client using `imap_tools` to fetch `headers_only=True` for unread mail.
- [ ] **Cache Check**: Logic to compare incoming `Message-ID` or `UID` against the database to prevent redundant processing.

## Phase 3: The Multi-Stage Triage Pipeline
- [ ] **Level 0 (Regex/Static)**: Filter out known noise (e.g., automated newsletters, marketing domains) using a Python whitelist/blacklist.
- [ ] **Level 1 (Local Triage)**: Pass the `Subject` + `Snippet` to a local model (e.g., Phi-3 or Gemma-2b via Ollama).
    - **Goal**: Binary classification (Important/Not Important).
    - **Cost**: 0 tokens.
- [ ] **Level 2 (Summarization)**: If Level 1 is "Important", send the full email body to the primary LLM (Gemini/LiteLLM) for a high-quality summary.

## Phase 4: Summarization & State Management
- [ ] **Daily Accumulator**: Store "Important" snippets in a structured daily JSON file.
- [ ] **Batch Summary**: At the end of the day, send the accumulated JSON to the LLM for the final daily digest.
- [ ] **Real-time Notifications**: If an email is classified as Level 1 "Important" during the hourly check, trigger an immediate system notification.

## Phase 5: Deployment & Monitoring
- [ ] **Dockerization**: Create a `Dockerfile` and `docker-compose.yml` to run the Python script and Redis.
- [ ] **Scheduling**: Set up a systemd timer or cron job to execute the check every 60 minutes.
- [ ] **Telemetry**: Add a simple logger to track how many emails were filtered at each stage vs. how many were sent to the cloud LLM.
