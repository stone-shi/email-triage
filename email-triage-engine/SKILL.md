---
name: email-triage-engine
description: Orchestrates robust stateless unread email ingestion from Gmail and IMAP, applies multi-stage triage (Level 0 regex, Level 1 ternary classification supporting dynamic downgrades, Level 2 premium summarization), tracks granular classification tags, reconstructs stored results via a Real Cache Layer, and outputs structured uniform JSON feeds. Use when you need to process unread emails, identify important messages, extract actions, or backfill dataset tags.
---

# Email Triage Engine

This skill provides procedural knowledge for running the Email Triage & Summarization Engine via `email-triage.sh` and interpreting its structured uniform JSON output feeds.

## Execution Workflow

The engine executes robust stateless scans of currently unread emails, querying SQLite cache checkpoints to reconstruct duplicate items instantly without dropping them from the final output array.

### Basic Usage

To fetch and process all unread emails from configured Gmail and IMAP accounts:

```bash
email-triage.sh
```

### Command-Line Arguments

- `--pretty`: Indents the standalone JSON output array for easier console viewing. Recommended for standard pipelines.
- `--human`: Renders a rich premium terminal interface displaying visual layout frames, level stamps, classification tags, match reasons, and telemetry summary tables. **Note: This suppresses raw JSON dumps.**
- `--max <n>`: Slices fetched unread email feeds to process only the top `n` items per ingestion source.
- `--days <n>`: Enforces a UTC-aware date cutoff to process only unread emails received within the last `N` days.
- `--level <n>`: Filters output to emit only items matching a specific triage level threshold or higher (e.g., `--level 2`).
- `--compact`: Emits a heavily minified JSON schema (`mid`, `lvl`, `tag`, `sum`) optimized for LLM token context windows.
- `--skip <n>` / `--limit <n>`: Enforces strict pagination offset slicing to process huge backlogs incrementally.
- `--output <path>`: Writes the full verbose JSON array directly to disk while emitting only a lightweight pointer summary to `stdout`.
- `--auth`: Purges existing OAuth persistent credentials (`token.json`) and forces a fresh authentication flow.
- `--headless`: Enables SSH console authentication. Redirects links to `stderr` and reads the landing address string from `stdin`.

## Interpreting JSON Output

`email-triage.sh` outputs a fully uniform JSON array where each element represents an unread email processed or reconstructed by the engine. Every dictionary contains identical core keys: `triage_level`, `message_id`, `account`, `sender`, `subject`, `date`, `reason`, `score`, and `tag`.

### Triage Levels & Classification Tags

1. **Level 0 (Filtered)**: Caught by static regex filters or dynamically downgraded by the Level 1 model.
2. **Level 1 (Unimportant)**: Evaluated by the ternary LLM as standard ambient notifications/promotions. Populates `"tag"` with granular extracted categories (e.g., `promotion`, `notification`, `personal`).
3. **Level 2 (Summarized)**: High priority actionable escalated items. Includes an extra `"summary"` field highlighting explicit tasks and deadlines.

For a detailed breakdown of the full uniform JSON schema and real cache behavior, see [references/output_schema.md](references/output_schema.md).

## Common Tasks

### 1. Daily Summary Extraction
Run `email-triage.sh` and filter the output list for `triage_level == 2` to generate a report of critical items.

### 2. Re-authentication
If Gmail ingestion fails with auth errors, run:
`email-triage.sh --auth --human` (or `--headless` if on a remote SSH server).

### 3. Missing Tag Dataset Backfill
To evaluate and backfill granular classification tags for existing offline benchmark datasets, use the standalone migration tool:
```bash
python3 add_missing_tags.py --profile production_deepseek_pair
```
