import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from config import settings

logger = logging.getLogger("email_triage.db")

class EmailDB:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = settings.workspace_dir / "email_cache.db"
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
                        tag TEXT
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
        """Check if a Message-ID has already been processed."""
        if not message_id:
            return False
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM email_cache WHERE message_id = ?", (message_id,))
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
        tag: Optional[str] = None
    ) -> None:
        """Save or update email triage results."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                processed_at = datetime.utcnow().isoformat()
                cursor.execute("""
                    INSERT OR REPLACE INTO email_cache 
                    (message_id, account, sender, subject, date_str, level_0_status, level_1_status, level_2_summary, 
                     reason, score, model_used_triage, model_used_summary, level_1_duration_sec, level_2_duration_sec, 
                     level_1_prompt_tokens, level_1_completion_tokens, level_2_prompt_tokens, level_2_completion_tokens, 
                     level_0_judge_correctness, level_0_judge_score, level_0_judge_reason, processed_at, triage_level, tag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (message_id, account, sender, subject, date_str, level_0_status, level_1_status, level_2_summary, 
                      reason, score, model_used_triage, model_used_summary, level_1_duration_sec, level_2_duration_sec, 
                      level_1_prompt_tokens, level_1_completion_tokens, level_2_prompt_tokens, level_2_completion_tokens, 
                      level_0_judge_correctness, level_0_judge_score, level_0_judge_reason, processed_at, triage_level, tag))
                conn.commit()
            logger.debug("Saved triage results for Message-ID: %s", message_id)
        except Exception as e:
            logger.error("Failed to save triage result for %s: %s", message_id, e, exc_info=True)

    def log_token_usage(self, event: str, model: str, tokens_used: int) -> None:
        """Log token consumption statistics for cloud LLM audit trail."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                timestamp = datetime.utcnow().isoformat()
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
