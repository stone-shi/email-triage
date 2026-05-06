---
name: email-triage-engine
description: Orchestrates email ingestion from Gmail and IMAP, applies multi-stage triage (Level 0 regex, Level 1 binary classification, Level 2 premium summarization), and outputs structured JSON results. Use when you need to process unread emails, identify important messages, and generate concise summaries for executive review.
---

# Email Triage Engine

This skill provides procedural knowledge for running the Email Triage & Summarization Engine via `main.py` and interpreting its structured JSON output. 

## Execution Workflow

The engine is designed to be run as a standalone script that emits a JSON array to `stdout`.

### Basic Usage

To fetch and process all unread emails from configured Gmail and IMAP accounts:

```bash
python main.py
```

### Command-Line Arguments

- `--pretty`: Indents the JSON output for easier reading. Recommended for debugging.
- `--human`: Renders a rich terminal interface with banners and metrics. Use this when a human is watching the console. **Note: This suppresses the raw JSON output.**
- `--auth`: Purges existing Gmail OAuth tokens and forces a fresh authentication flow.
- `--headless`: Enables SSH-friendly authentication. Redirects links to `stderr` and reads the landing URL from `stdin`.

## Interpreting JSON Output

By default, `main.py` outputs a JSON list where each element represents an email processed by the engine (unless it was skipped by the cache).

### Triage Levels

1.  **Level 0 (Filtered)**: Caught by static regex filters (noise/spam).
2.  **Level 1 (Unimportant)**: Evaluated by a fast LLM and deemed not important.
3.  **Level 2 (Summarized)**: Deemed important by Level 1 and summarized by a premium LLM.

For a detailed breakdown of the JSON schema for each level, see [references/output_schema.md](references/output_schema.md).

## Common Tasks

### 1. Daily Summary
Run `python main.py` and filter the results for `triage_level == "Level 2"` to generate a report of important items.

### 2. Re-authentication
If Gmail ingestion fails with auth errors, instruct the user to run:
`python main.py --auth --human` (or `--headless` if on a remote server).

### 3. Debugging Noise Filters
If a legitimate email was caught by Level 0, search for it in the JSON output to find the specific `reason` (keyword match) and update `config.py` if necessary.
