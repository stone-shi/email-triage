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
- **Level 0 (`triage_level: 0`)**: Intercepted by static noise pre-filters or **dynamically downgraded** by the Level 1 model. `score` reflects model confidence on downgrades (or `0.0` for regex hits), and `tag` captures the model's suggested string (or `"low"`).
- **Level 1 (`triage_level: 1`)**: Classified as standard ambient notifications/promotions by fast ternary triage. `score` reflects model confidence, and `tag` contains the model-generated category.
- **Level 2 (`triage_level: 2`)**: High-priority actionable/personal threads. Includes all standard keys plus an additional `"summary"` field containing a bulleted executive action brief.

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

## Compact Mode JSON Schema (`--compact`)
Invoking `./triage.sh --compact` heavily minifies key strings and purges verbose justification reason descriptions to optimize context window consumption for LLM tools. The keys map exactly as follows:

| Compact Key | Full Mode Key | Data Type | Description |
| :--- | :--- | :--- | :--- |
| **`mid`** | `message_id` | `str` | Unique RFC 2822 global identifier string. |
| **`lvl`** | `triage_level` | `int` | Priority tier integer (`0`, `1`, or `2`). |
| **`snd`** | `sender` | `str` | From envelope header string. |
| **`sub`** | `subject` | `str` | Raw Subject line. |
| **`dt`** | `date` | `str` | Arrival date string. |
| **`tag`** | `tag` | `str` | One-word classification category. |
| **`sum`** | `summary` | `str` | *(Level 2 Only)* Bulleted executive action brief. |

## Real Cache Reconstruction Behavior
The engine treats the SQLite persistence database (`email_cache.db`) as a **Real Cache Layer**. If an incoming unread email matches an existing database entry, the system instantly reconstructs the full uniform dictionary from the stored row, logs the human notification card, and appends the object directly into the final JSON response feed at zero network/token cost.
