import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, Dict
from config import settings

logger = logging.getLogger("email_triage.db")

class EmailDB:
    def __init__(self, db_path: Optional[Path] = None, settings_instance: Optional[Any] = None):
        if db_path is None:
            active_settings = settings_instance if settings_instance else settings
            db_path = active_settings.workspace_dir / "email_cache.db"
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30.0)

    def _init_db(self) -> None:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Create cache & triage tracking table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS email_cache (
                        message_id TEXT PRIMARY KEY,
                        account TEXT NOT NULL,
                        sender TEXT,
                        subject TEXT,
                        date_str TEXT,
                        level_0_status TEXT, -- 'passed' or 'filtered'
                        level_1_status TEXT, -- 'important' or 'unimportant' or 'skipped'
                        level_2_summary TEXT,
                        reason TEXT,
                        score REAL,
                        model_used_triage TEXT,
                        model_used_summary TEXT,
                        level_1_duration_sec REAL,
                        level_2_duration_sec REAL,
                        level_1_prompt_tokens INTEGER,
                        level_1_completion_tokens INTEGER,
                        level_2_prompt_tokens INTEGER,
                        level_2_completion_tokens INTEGER,
                        level_0_judge_correctness TEXT,
                        level_0_judge_score REAL,
                        level_0_judge_reason TEXT,
                        processed_at TEXT NOT NULL,
                        triage_level INTEGER,
                        tag TEXT,
                        email_body TEXT,
                        tei_enabled INTEGER,
                        tei_score REAL,
                        tei_decision TEXT,
                        level_1_run INTEGER,
                        level_1_model TEXT,
                        level_1_score REAL,
                        level_2_run INTEGER,
                        level_2_model TEXT
                    )
                """)
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN triage_level INTEGER")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN tag TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN email_body TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN tei_enabled INTEGER")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN tei_score REAL")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN tei_decision TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN level_1_run INTEGER")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN level_1_model TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN level_1_score REAL")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN level_2_run INTEGER")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN level_2_model TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN snippet TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN is_unread INTEGER")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN source_id TEXT")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE email_cache ADD COLUMN downloaded_at TEXT")
                except Exception:
                    pass
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_email_cache_is_unread ON email_cache(is_unread, account)"
                )
                # Create basic metrics/tokens log table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS token_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event TEXT NOT NULL,
                        model TEXT,
                        tokens_used INTEGER,
                        timestamp TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sync_state (
                        account TEXT PRIMARY KEY,
                        checkpoint_val TEXT NOT NULL
                    )
                """)
                conn.commit()
            logger.info("SQLite Database initialized at %s", self.db_path)
        except Exception as e:
            logger.error("Failed to initialize database: %s", e, exc_info=True)
            raise

    def is_processed(self, message_id: str) -> bool:
        """Check if a Message-ID has already been triaged (a row may exist pre-triage from a download-only pass)."""
        if not message_id:
            return False
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM email_cache WHERE message_id = ? AND triage_level IS NOT NULL", (message_id,)
                )
                return cursor.fetchone() is not None
        except Exception as e:
            logger.error("Error checking message_id cache: %s", e)
            return False

    def get_cached_result(self, message_id: str) -> Optional[dict]:
        """Retrieve a full cached email record as a dictionary by Message-ID."""
        if not message_id:
            return None
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM email_cache WHERE message_id = ?", (message_id,))
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None
        except Exception as e:
            logger.error("Error fetching cached record details: %s", e)
            return None

    def get_sync_checkpoint(self, account: str) -> Optional[str]:
        """Retrieve the last stored delta synchronization checkpoint token for an account."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT checkpoint_val FROM sync_state WHERE account = ?", (account,))
                row = cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error("Error fetching sync checkpoint for %s: %s", account, e)
            return None

    def save_sync_checkpoint(self, account: str, val: str) -> None:
        """Persist the latest delta synchronization checkpoint token for an account."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO sync_state (account, checkpoint_val) VALUES (?, ?)", (account, str(val)))
                conn.commit()
        except Exception as e:
            logger.error("Error saving sync checkpoint for %s: %s", account, e)

    def save_sync_summary(self, account: str, summary: dict) -> None:
        """Persist a JSON-encoded last-download summary (timestamp/counts/errors) for an account."""
        import json
        self.save_sync_checkpoint(account, json.dumps(summary))

    def get_sync_summary(self, account: str) -> Optional[dict]:
        """Retrieve the last-download summary previously saved via save_sync_summary, if any."""
        import json
        val = self.get_sync_checkpoint(account)
        if not val:
            return None
        try:
            return json.loads(val)
        except Exception:
            return None

    def upsert_email_metadata(
        self,
        message_id: str,
        account: str,
        sender: Optional[str] = None,
        subject: Optional[str] = None,
        date_str: Optional[str] = None,
        snippet: Optional[str] = None,
        source_id: Optional[str] = None,
        email_body: Optional[str] = None,
        is_unread: Optional[bool] = None,
    ) -> None:
        """
        Download-phase upsert: records mailbox metadata/content for a message ahead of triage,
        without touching any triage-result columns. Safe to call repeatedly (e.g. every sync tick) —
        unset fields are preserved via COALESCE rather than overwritten with NULL.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                now = datetime.now(tz=timezone.utc).isoformat()
                is_unread_int = 1 if is_unread is True else (0 if is_unread is False else None)
                cursor.execute("""
                    INSERT INTO email_cache
                    (message_id, account, sender, subject, date_str, snippet, source_id, email_body,
                     is_unread, downloaded_at, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        account = excluded.account,
                        sender = COALESCE(excluded.sender, email_cache.sender),
                        subject = COALESCE(excluded.subject, email_cache.subject),
                        date_str = COALESCE(excluded.date_str, email_cache.date_str),
                        snippet = COALESCE(excluded.snippet, email_cache.snippet),
                        source_id = COALESCE(excluded.source_id, email_cache.source_id),
                        email_body = COALESCE(excluded.email_body, email_cache.email_body),
                        is_unread = COALESCE(excluded.is_unread, email_cache.is_unread),
                        downloaded_at = excluded.downloaded_at
                """, (message_id, account, sender, subject, date_str, snippet, source_id, email_body,
                      is_unread_int, now, now))
                conn.commit()
        except Exception as e:
            logger.error("Failed to upsert email metadata for %s: %s", message_id, e, exc_info=True)

    def get_unread_emails(self, account: Optional[str] = None, limit: Optional[int] = None) -> list:
        """Retrieve cached emails currently flagged unread, most recently processed first."""
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                query = "SELECT * FROM email_cache WHERE is_unread = 1"
                params: list = []
                if account:
                    query += " AND account = ?"
                    params.append(account)
                query += " ORDER BY processed_at DESC"
                if limit is not None:
                    query += " LIMIT ?"
                    params.append(limit)
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch unread emails: %s", e)
            return []

    def get_unread_message_ids(self, account: str) -> set:
        """Retrieve the set of message_ids currently cached as unread for an account."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT message_id FROM email_cache WHERE account = ? AND is_unread = 1", (account,)
                )
                return {row[0] for row in cursor.fetchall()}
        except Exception as e:
            logger.error("Failed to fetch unread message ids for %s: %s", account, e)
            return set()

    def get_email_counts(self, account: Optional[str] = None) -> Dict[str, int]:
        """Aggregate cached-email counts by triage level, optionally scoped to one account."""
        counts = {"total": 0, "level_0": 0, "level_1": 0, "level_2": 0, "pending_triage": 0}
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT triage_level, COUNT(*) FROM email_cache"
                params: list = []
                if account:
                    query += " WHERE account = ?"
                    params.append(account)
                query += " GROUP BY triage_level"
                cursor.execute(query, params)
                for level, cnt in cursor.fetchall():
                    counts["total"] += cnt
                    if level is None:
                        counts["pending_triage"] = cnt
                    elif level in (0, 1, 2):
                        counts[f"level_{level}"] = cnt
            return counts
        except Exception as e:
            logger.error("Failed to get email counts for %s: %s", account or "all accounts", e)
            return counts

    def save_triage_result(
        self,
        message_id: str,
        account: str,
        sender: str,
        subject: str,
        date_str: str,
        level_0_status: str,
        level_1_status: str = "skipped",
        level_2_summary: Optional[str] = None,
        reason: Optional[str] = None,
        score: Optional[float] = None,
        model_used_triage: Optional[str] = None,
        model_used_summary: Optional[str] = None,
        level_1_duration_sec: Optional[float] = None,
        level_2_duration_sec: Optional[float] = None,
        level_1_prompt_tokens: Optional[int] = None,
        level_1_completion_tokens: Optional[int] = None,
        level_2_prompt_tokens: Optional[int] = None,
        level_2_completion_tokens: Optional[int] = None,
        level_0_judge_correctness: Optional[str] = None,
        level_0_judge_score: Optional[float] = None,
        level_0_judge_reason: Optional[str] = None,
        triage_level: Optional[int] = None,
        tag: Optional[str] = None,
        email_body: Optional[str] = None,
        tei_enabled: Optional[bool] = None,
        tei_score: Optional[float] = None,
        tei_decision: Optional[str] = None,
        level_1_run: Optional[bool] = None,
        level_1_model: Optional[str] = None,
        level_1_score: Optional[float] = None,
        level_2_run: Optional[bool] = None,
        level_2_model: Optional[str] = None
    ) -> None:
        """Save or update email triage results."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                processed_at = datetime.now(tz=timezone.utc).isoformat()
                
                # Convert boolean inputs to integer for SQLite compatibility
                tei_enabled_int = 1 if tei_enabled is True else (0 if tei_enabled is False else None)
                level_1_run_int = 1 if level_1_run is True else (0 if level_1_run is False else None)
                level_2_run_int = 1 if level_2_run is True else (0 if level_2_run is False else None)
                
                cursor.execute("""
                    INSERT INTO email_cache
                    (message_id, account, sender, subject, date_str, level_0_status, level_1_status, level_2_summary,
                     reason, score, model_used_triage, model_used_summary, level_1_duration_sec, level_2_duration_sec,
                     level_1_prompt_tokens, level_1_completion_tokens, level_2_prompt_tokens, level_2_completion_tokens,
                     level_0_judge_correctness, level_0_judge_score, level_0_judge_reason, processed_at, triage_level, tag,
                     email_body, tei_enabled, tei_score, tei_decision, level_1_run, level_1_model, level_1_score,
                     level_2_run, level_2_model)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        account = excluded.account,
                        sender = excluded.sender,
                        subject = excluded.subject,
                        date_str = excluded.date_str,
                        level_0_status = excluded.level_0_status,
                        level_1_status = excluded.level_1_status,
                        level_2_summary = excluded.level_2_summary,
                        reason = excluded.reason,
                        score = excluded.score,
                        model_used_triage = excluded.model_used_triage,
                        model_used_summary = excluded.model_used_summary,
                        level_1_duration_sec = excluded.level_1_duration_sec,
                        level_2_duration_sec = excluded.level_2_duration_sec,
                        level_1_prompt_tokens = excluded.level_1_prompt_tokens,
                        level_1_completion_tokens = excluded.level_1_completion_tokens,
                        level_2_prompt_tokens = excluded.level_2_prompt_tokens,
                        level_2_completion_tokens = excluded.level_2_completion_tokens,
                        level_0_judge_correctness = excluded.level_0_judge_correctness,
                        level_0_judge_score = excluded.level_0_judge_score,
                        level_0_judge_reason = excluded.level_0_judge_reason,
                        processed_at = excluded.processed_at,
                        triage_level = excluded.triage_level,
                        tag = excluded.tag,
                        email_body = COALESCE(excluded.email_body, email_cache.email_body),
                        tei_enabled = excluded.tei_enabled,
                        tei_score = excluded.tei_score,
                        tei_decision = excluded.tei_decision,
                        level_1_run = excluded.level_1_run,
                        level_1_model = excluded.level_1_model,
                        level_1_score = excluded.level_1_score,
                        level_2_run = excluded.level_2_run,
                        level_2_model = excluded.level_2_model
                """, (message_id, account, sender, subject, date_str, level_0_status, level_1_status, level_2_summary,
                      reason, score, model_used_triage, model_used_summary, level_1_duration_sec, level_2_duration_sec,
                      level_1_prompt_tokens, level_1_completion_tokens, level_2_prompt_tokens, level_2_completion_tokens,
                      level_0_judge_correctness, level_0_judge_score, level_0_judge_reason, processed_at, triage_level, tag,
                      email_body, tei_enabled_int, tei_score, tei_decision, level_1_run_int, level_1_model, level_1_score,
                      level_2_run_int, level_2_model))
                conn.commit()
            logger.debug("Saved triage results for Message-ID: %s", message_id)
        except Exception as e:
            logger.error("Failed to save triage result for %s: %s", message_id, e, exc_info=True)

    def log_token_usage(self, event: str, model: str, tokens_used: int) -> None:
        """Log token consumption statistics for cloud LLM audit trail."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                timestamp = datetime.now(tz=timezone.utc).isoformat()
                cursor.execute("""
                    INSERT INTO token_logs (event, model, tokens_used, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (event, model, tokens_used, timestamp))
                conn.commit()
        except Exception as e:
            logger.error("Failed to log token usage: %s", e)

    def get_daily_important_emails(self) -> list:
        """Retrieve all emails marked important for the current day digest."""
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT sender, subject, level_2_summary, date_str 
                    FROM email_cache 
                    WHERE level_1_status = 'important' 
                    ORDER BY processed_at DESC
                """)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch daily important emails: %s", e)
            return []
