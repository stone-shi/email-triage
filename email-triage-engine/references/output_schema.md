# Output JSON Schema Reference

The engine returns a fully uniform JSON array of objects representing all scanned unread emails (including reconstructed cache hits). Every dictionary consistently contains the same core set of attributes to ensure robust downstream interpretation.

## Full Uniform JSON Schema

```json
{
  "triage_level": "integer (0, 1, or 2)",
  "message_id": "string (RFC 2822 Message-ID)",
  "account": "string (ingestion account email)",
  "sender": "string (From header)",
  "subject": "string (Subject header)",
  "date": "string (envelope arrival date string)",
  "reason": "string (rule match, LLM justification, or cache note)",
  "score": "float (confidence score from 0.0 to 1.0)",
  "tag": "string (one-word classification, e.g., 'low', 'promotion', 'notification', 'personal', 'vip')"
}
```

### Triage Level Specific Variations
- **Level 0 (`triage_level: 0`)**: Intercepted by static noise pre-filters. `score` defaults to `0.0`, and `tag` is hardcoded to `"low"`.
- **Level 1 (`triage_level: 1`)**: Classified as unimportant by fast binary triage. `score` reflects model confidence, and `tag` contains the model-generated classification string.
- **Level 2 (`triage_level: 2`)**: High-priority escalated emails. Includes all standard keys plus an additional `"summary"` field containing a bulleted executive action brief.

```json
{
  "triage_level": 2,
  "message_id": "<foo@bar.com>",
  "account": "your_email@gmail.com",
  "sender": "CEO <ceo@company.com>",
  "subject": "URGENT: Board meeting presentation",
  "date": "Fri, 8 May 2026 14:00:00 -0700",
  "reason": "Direct request from executive leader requiring action",
  "score": 0.98,
  "tag": "vip",
  "summary": "- Review slides attached before 4 PM today.\n- Confirm attendance via calendar invite."
}
```

## Real Cache Reconstruction Behavior
The engine treats the SQLite persistence database (`email_cache.db`) as a **Real Cache Layer**. If an incoming unread email matches an existing database entry, the system instantly reconstructs the full uniform dictionary from the stored row, logs the human notification card, and appends the object directly into the final JSON response feed at zero network/token cost.
