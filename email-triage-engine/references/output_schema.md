# Output JSON Schema Reference

The engine returns a list of JSON objects. The fields present in each object depend on the `triage_level`.

## Level 0: Static Pre-Filter
Emails caught by the noise blacklist (senders or subjects).

```json
{
  "triage_level": "Level 0",
  "message_id": "string",
  "account": "string (gmail|imap)",
  "subject": "string",
  "reason": "string (e.g., 'Static filter hit: noise keyword \'unsubscribe\' matched')"
}
```

## Level 1: Lightweight Triage
Emails deemed "unimportant" by the triage model.

```json
{
  "triage_level": "Level 1",
  "message_id": "string",
  "account": "string",
  "subject": "string",
  "reason": "string (LLM justification)",
  "score": "float (confidence 0.0-1.0)"
}
```

## Level 2: Premium Summarization
Important emails that have been summarized.

```json
{
  "triage_level": "Level 2",
  "message_id": "string",
  "account": "string",
  "sender": "string",
  "subject": "string",
  "date": "string (ISO or formatted)",
  "reason": "string (Level 1 justification)",
  "summary": "string (High-fidelity bulleted summary)",
  "score": "float (Level 2 confidence score)"
}
```

## Cache Behavior
If an email's `Message-ID` is already present in `email_cache.db`, it is skipped entirely and will **not** appear in the JSON output array. To see cache hits, you must use the `--human` flag.
