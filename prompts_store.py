"""Global (admin-editable) LLM system prompts for the live triage pipeline.

Stored in data/app.db's existing app_settings table under a `prompt.<key>`
namespace -- a parallel concern to app_settings_store.py's RUNTIME_KEYS, but
these are multi-line text blobs that don't overlay onto a Settings instance
the way scalar config does, so they get their own small store instead of
joining that registry.

Precedence when EmailTriageEngine builds its effective prompt set (see
triage.py): DB (this module) > prompts.yml > DEFAULT_PROMPTS below. The
existing prompts.yml file is kept as the pre-DB fallback, matching every
other DB-vs-legacy path in this codebase -- nothing on disk is deleted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Optional

import yaml

from appdb import utcnow

_KEY_PREFIX = "prompt."

PROMPT_LABELS: Dict[str, str] = {
    "level_1_fast_triage": "Level 1 — Fast triage",
    "level_1_premium_escalation": "Level 1 — Premium escalation",
    "level_2_summarization": "Level 2 — Executive summary",
}

PROMPT_DESCRIPTIONS: Dict[str, str] = {
    "level_1_fast_triage": "Cheap ternary classifier that runs on every email (From/Subject/Snippet only, no body fetch).",
    "level_1_premium_escalation": "Premium re-check run only when Level 1's confidence score is below the configured threshold.",
    "level_2_summarization": "Executive summary generated for Level 2 (important) emails, after the full body is fetched.",
}

# Single source of truth for the hardcoded fallback text -- triage.py imports
# these instead of duplicating the strings inline.
DEFAULT_PROMPTS: Dict[str, str] = {
    "level_1_fast_triage": (
        "You are an expert executive assistant evaluating an email to suggest its triage level.\n"
        "Output suggested_level as an integer:\n"
        "0 - pure noise, random promotion, social media notification not directly addressed to user, notification requiring no action.\n"
        "1 - notification worth reviewing, promotion addressing user (e.g., birthday credit, coupon, free credit).\n"
        "2 - important, actionable, personal human conversation or critical alert.\n"
        "You MUST return a valid JSON object containing exactly four fields: "
        "'suggested_level' (integer: 0, 1, or 2), 'reason' (string explaining the level), 'confidence_score' (float from 0.0 to 1.0), and "
        "'tag' (a one word lowercase tag, e.g., \"promotion\", \"notification\", \"personal\", \"vip\", \"low\")."
    ),
    "level_1_premium_escalation": (
        "You are a premium AI operations auditor re-evaluating an ambiguous email triage level query.\n"
        "Output suggested_level as an integer:\n"
        "0 - pure noise, random promotion, social media notification not directly addressed to user, notification requiring no action.\n"
        "1 - notification worth reviewing, promotion addressing user (e.g., birthday credit, coupon, free credit).\n"
        "2 - important, actionable, personal human conversation or critical alert.\n"
        "You MUST return a valid JSON object containing exactly four fields: "
        "'suggested_level' (integer: 0, 1, or 2), 'reason' (string), 'confidence_score' (float from 0.0 to 1.0), and "
        "'tag' (a one word lowercase tag, e.g., \"personal\", \"vip\", \"promotion\", \"notification\")."
    ),
    "level_2_summarization": (
        "Create clear, precise bulleted executive summaries. Be brief and highlight any requested task, conclusion, or deadline. "
        "You MUST return a valid JSON object containing exactly three fields: 'summary' (string), 'confidence_score' (float from 0.0 to 1.0), "
        "and 'tag' (a one word lowercase tag, e.g., \"personal\", \"vip\", \"update\")."
    ),
}


def _storage_key(key: str) -> str:
    return f"{_KEY_PREFIX}{key}"


def get_prompt(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_storage_key(key),)).fetchone()
    return row["value"] if row is not None else None


def get_all_prompts_raw(conn: sqlite3.Connection) -> Dict[str, str]:
    """DB-only overrides, keyed by prompt name -- what EmailTriageEngine overlays on top of prompts.yml."""
    return {key: value for key in DEFAULT_PROMPTS if (value := get_prompt(conn, key)) is not None}


def set_prompt(conn: sqlite3.Connection, key: str, value: str, *, updated_by: Optional[int] = None) -> None:
    if key not in DEFAULT_PROMPTS:
        raise ValueError(f"Unknown prompt key {key!r}")
    conn.execute(
        """
        INSERT INTO app_settings (key, value, value_type, is_secret, updated_by, updated_at)
        VALUES (?, ?, 'str', 0, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at
        """,
        (_storage_key(key), value, updated_by, utcnow()),
    )


def reset_prompt(conn: sqlite3.Connection, key: str) -> None:
    """Deletes the DB override so the effective value falls back through
    prompts.yml, then the hardcoded default -- not a copy of either, so it
    stays correct if prompts.yml changes later."""
    conn.execute("DELETE FROM app_settings WHERE key = ?", (_storage_key(key),))


def load_yaml_prompts(yaml_path: Path) -> Dict[str, str]:
    try:
        if not yaml_path.exists():
            return {}
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {k: v["system"] for k, v in data.items() if isinstance(v, dict) and v.get("system")}
    except Exception:
        return {}


def get_all_prompts(conn: sqlite3.Connection, *, yaml_path: Optional[Path] = None) -> Dict[str, dict]:
    """Every known prompt key with its effective value and where it came from."""
    yaml_prompts = load_yaml_prompts(yaml_path) if yaml_path else {}
    out = {}
    for key in DEFAULT_PROMPTS:
        db_value = get_prompt(conn, key)
        if db_value is not None:
            value, source = db_value, "database"
        elif key in yaml_prompts:
            value, source = yaml_prompts[key], "prompts.yml"
        else:
            value, source = DEFAULT_PROMPTS[key], "default"
        out[key] = {
            "label": PROMPT_LABELS.get(key, key),
            "description": PROMPT_DESCRIPTIONS.get(key, ""),
            "value": value,
            "source": source,
        }
    return out


def seed_from_yaml_or_defaults(conn: sqlite3.Connection, yaml_path: Optional[Path] = None) -> int:
    """Idempotent, per-key: any prompt key not already present in the DB gets
    seeded from prompts.yml (if it defines that key), else the hardcoded
    default. Existing DB rows -- including anything an admin has already
    edited -- are never touched. Returns the number of keys seeded, so a
    caller can log it once at startup."""
    yaml_prompts = load_yaml_prompts(yaml_path) if yaml_path else {}
    seeded = 0
    for key in DEFAULT_PROMPTS:
        if get_prompt(conn, key) is not None:
            continue
        set_prompt(conn, key, yaml_prompts.get(key, DEFAULT_PROMPTS[key]))
        seeded += 1
    return seeded
