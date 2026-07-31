import os
import yaml
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Shared placeholder sentinels for "this account was never configured" -- used
# by mcp_server.py's dashboard skip-logic and by account_clients.py's legacy
# (pre-migration) single-account fallback to decide whether a profile is real.
PLACEHOLDER_GMAIL_ACCOUNT = "your_email@gmail.com"
PLACEHOLDER_IMAP_LOGIN = "your_email@domain.com"


def parse_duration(value, default_seconds: float = 900.0) -> float:
    """Parses a duration expressed as seconds (int/float) or a suffixed string like '15m'/'1h'/'45s'/'1d'."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return default_seconds
    if s[-1] in _DURATION_UNITS:
        try:
            return float(s[:-1]) * _DURATION_UNITS[s[-1]]
        except ValueError:
            return default_seconds
    try:
        return float(s)
    except ValueError:
        return default_seconds


def list_profile_names() -> List[str]:
    """Lists all configured profile names (subdirectories under profiles/), always including 'default'."""
    workspace_root = Path(__file__).parent.resolve()
    profiles_dir = workspace_root / "profiles"
    names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.exists() else []
    if "default" not in names:
        names.append("default")
    return names


class SchedulerSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_SCHEDULER_ENABLED", "true").strip().lower()
        not in ("false", "0", "no", "")
    )
    interval: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_SCHEDULER_INTERVAL", "15m"))
    max_per_account: Optional[int] = Field(
        default_factory=lambda: (
            int(os.environ["EMAIL_TRIAGE_SCHEDULER_MAX_PER_ACCOUNT"])
            if os.getenv("EMAIL_TRIAGE_SCHEDULER_MAX_PER_ACCOUNT")
            else None
        )
    )
    days: Optional[int] = Field(
        default_factory=lambda: (
            int(os.environ["EMAIL_TRIAGE_SCHEDULER_DAYS"]) if os.getenv("EMAIL_TRIAGE_SCHEDULER_DAYS") else None
        )
    )

    @property
    def interval_seconds(self) -> float:
        return parse_duration(self.interval)


class DownloadAllSchedulerSettings(BaseModel):
    """Recurring trigger for the full-mailbox archive downloader ("Download All"). Defaults to
    enabled/nightly -- after the first (expensive) full backfill, subsequent runs are cheap since
    already-archived messages are skipped, so a nightly cadence just picks up new mail."""
    enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_DOWNLOAD_ALL_SCHEDULER_ENABLED", "true").strip().lower()
        not in ("false", "0", "no", "")
    )
    interval: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_DOWNLOAD_ALL_SCHEDULER_INTERVAL", "24h"))

    @property
    def interval_seconds(self) -> float:
        return parse_duration(self.interval, default_seconds=86400.0)


class AutoMarkReadLevel0Settings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_0_ENABLED", "false").strip().lower()
        not in ("false", "0", "no", "")
    )
    after_displays: int = Field(
        default_factory=lambda: (
            int(os.environ["EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_0_AFTER_DISPLAYS"])
            if os.getenv("EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_0_AFTER_DISPLAYS")
            else 1
        )
    )


class AutoMarkReadLevel1Settings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_1_ENABLED", "false").strip().lower()
        not in ("false", "0", "no", "")
    )
    after_displays: int = Field(
        default_factory=lambda: (
            int(os.environ["EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_1_AFTER_DISPLAYS"])
            if os.getenv("EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_1_AFTER_DISPLAYS")
            else 1
        )
    )


class AutoMarkReadLevel2Settings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_2_ENABLED", "false").strip().lower()
        not in ("false", "0", "no", "")
    )
    after_displays: int = Field(
        default_factory=lambda: (
            int(os.environ["EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_2_AFTER_DISPLAYS"])
            if os.getenv("EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_2_AFTER_DISPLAYS")
            else 1
        )
    )


class AutoMarkReadSettings(BaseModel):
    """Each triage level is independently configured -- e.g. level 0 (noise) can be auto-marked
    read after a single view while level 2 (important) stays off entirely."""
    level_0: AutoMarkReadLevel0Settings = Field(default_factory=AutoMarkReadLevel0Settings)
    level_1: AutoMarkReadLevel1Settings = Field(default_factory=AutoMarkReadLevel1Settings)
    level_2: AutoMarkReadLevel2Settings = Field(default_factory=AutoMarkReadLevel2Settings)


class QualityCheckSettings(BaseModel):
    """Nightly 'no-look' production quality audit: re-runs a random sample of
    already-triaged messages through a separately-configured judge LLM and
    compares the judge's independent decision against what production
    actually decided, to catch triage/summary drift without a human reading
    every email. Disabled by default -- it costs judge-model tokens and
    requires a judge endpoint/model to be configured first."""
    enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_ENABLED", "false").strip().lower()
        not in ("false", "0", "no", "")
    )
    hour: int = Field(default_factory=lambda: int(os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_HOUR", "1")))
    minute: int = Field(default_factory=lambda: int(os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_MINUTE", "0")))
    sample_rate: float = Field(
        default_factory=lambda: float(os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_SAMPLE_RATE", "0.10"))
    )
    judge_base_url: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_JUDGE_BASE_URL", ""))
    judge_model: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_JUDGE_MODEL", ""))
    judge_api_key: str = Field(
        default_factory=lambda: os.getenv("EMAIL_TRIAGE_QUALITY_CHECK_JUDGE_API_KEY", "")
    )


class TriageSettings(BaseModel):
    confidence_threshold: float = 0.8
    triage_type: str = "llm"
    tei_url: str = "https://omniroute.local.shifamily.com/v1/rerank"
    tei_model: str = "localai/qwen3-reranker-0.6b"
    tei_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_TEI_API_KEY", ""))
    tei_router_enabled: bool = False
    tei_noise_enabled: bool = True
    tei_signal_enabled: bool = True
    tei_noise_threshold: float = 0.999
    tei_signal_threshold: float = 0.95
    whitelist_vip_senders: List[str] = []
    whitelist_domains: List[str] = []
    blacklist_keywords: List[str] = [
        "unsubscribe", "newsletter", "promotions", "marketing", 
        "no-reply", "noreply", "digest", "advertisement"
    ]
    blacklist_senders: List[str] = [
        "spammer@domain.com", "offers@", "newsletters@"
    ]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EMAIL_TRIAGE_",
        extra="ignore"
    )

    workspace_dir: Path = Field(default_factory=lambda: Path(__file__).parent.resolve())
    
    # Operational attributes loaded from YAML
    gmail_credentials_path: Path = Path("credentials.json")
    gmail_token_path: Path = Field(default_factory=lambda: Path(__file__).parent.resolve() / "token.json")
    gmail_account: str = "your_email@gmail.com"
    headless_mode: bool = False

    imap_host: str = "imap.zoho.com"
    imap_port: int = 993
    imap_login: str = "your_email@domain.com"
    imap_password: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_IMAP_PASSWORD", ""))

    smtp_host: str = "smtp.zoho.com"
    smtp_port: int = 465
    smtp_login: Optional[str] = None
    smtp_password: Optional[str] = None

    @property
    def active_smtp_login(self) -> str:
        return self.smtp_login if self.smtp_login else self.imap_login

    @property
    def active_smtp_password(self) -> str:
        return self.smtp_password if self.smtp_password else self.imap_password

    triage: TriageSettings = Field(default_factory=TriageSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    download_all_scheduler: DownloadAllSchedulerSettings = Field(default_factory=DownloadAllSchedulerSettings)
    auto_mark_read: AutoMarkReadSettings = Field(default_factory=AutoMarkReadSettings)
    quality_check: QualityCheckSettings = Field(default_factory=QualityCheckSettings)

    triage_base_url: str = "https://your-llm-proxy.com/v1"
    summary_base_url: str = "https://your-llm-proxy.com/v1"
    triage_model: str = "deepseek/deepseek-v4-flash"
    summary_model: str = "deepseek/deepseek-v4-pro"
    log_level: str = "INFO"
    tei_url: str = "https://omniroute.local.shifamily.com/v1/rerank"
    tei_model: str = "localai/qwen3-reranker-0.6b"

    # MCP Server settings
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # API Secret Keys kept strictly in environment context
    gemini_api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    triage_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_TRIAGE_API_KEY", os.getenv("EMAIL_TRIAGE_LLM_API_KEY", "")))
    summary_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_SUMMARY_API_KEY", os.getenv("EMAIL_TRIAGE_LLM_API_KEY", "")))
    tei_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_TEI_API_KEY", ""))

    @property
    def llm_base_url(self) -> str:
        return self.triage_base_url

    @property
    def llm_api_key(self) -> str:
        return self.triage_api_key

    @model_validator(mode="after")
    def sync_triage_settings(self) -> "Settings":
        if hasattr(self, "tei_url") and self.tei_url:
            self.triage.tei_url = self.tei_url
        if hasattr(self, "tei_model") and self.tei_model:
            self.triage.tei_model = self.tei_model
        if hasattr(self, "tei_api_key") and self.tei_api_key:
            self.triage.tei_api_key = self.tei_api_key
        return self

    def load_from_yaml(self, yaml_path: Optional[Path] = None, env_file: Optional[Path] = None) -> None:
        if yaml_path is None:
            yaml_path = self.workspace_dir / "config.yml"
        if yaml_path.exists():
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f) or {}
                
                # Identify all active environment variable keys (from os.environ and the .env file)
                active_env_keys = set(os.environ.keys())
                if env_file and env_file.exists():
                    try:
                        with open(env_file, "r", encoding="utf-8") as env_f:
                            for line in env_f:
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k, _ = line.split("=", 1)
                                    active_env_keys.add(k.strip())
                    except Exception:
                        pass

                def should_apply(field_env_name: str) -> bool:
                    return field_env_name not in active_env_keys

                # Map LLM section
                llm_data = yaml_data.get("llm", {})
                if "base_url" in llm_data:
                    if should_apply("EMAIL_TRIAGE_TRIAGE_BASE_URL"):
                        self.triage_base_url = llm_data["base_url"]
                    if should_apply("EMAIL_TRIAGE_SUMMARY_BASE_URL"):
                        self.summary_base_url = llm_data["base_url"]
                if "triage_base_url" in llm_data and should_apply("EMAIL_TRIAGE_TRIAGE_BASE_URL"):
                    self.triage_base_url = llm_data["triage_base_url"]
                if "summary_base_url" in llm_data and should_apply("EMAIL_TRIAGE_SUMMARY_BASE_URL"):
                    self.summary_base_url = llm_data["summary_base_url"]
                if "triage_api_key" in llm_data and should_apply("EMAIL_TRIAGE_TRIAGE_API_KEY"):
                    self.triage_api_key = llm_data["triage_api_key"]
                if "summary_api_key" in llm_data and should_apply("EMAIL_TRIAGE_SUMMARY_API_KEY"):
                    self.summary_api_key = llm_data["summary_api_key"]
                if "triage_model" in llm_data and should_apply("EMAIL_TRIAGE_TRIAGE_MODEL"):
                    self.triage_model = llm_data["triage_model"]
                if "summary_model" in llm_data and should_apply("EMAIL_TRIAGE_SUMMARY_MODEL"):
                    self.summary_model = llm_data["summary_model"]
                
                # Map Triage section
                triage_data = yaml_data.get("triage", {})
                if "confidence_threshold" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__CONFIDENCE_THRESHOLD"):
                    self.triage.confidence_threshold = float(triage_data["confidence_threshold"])
                if "triage_type" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TRIAGE_TYPE"):
                    self.triage.triage_type = triage_data["triage_type"]
                if "tei_url" in triage_data and should_apply("EMAIL_TRIAGE_TEI_URL"):
                    self.triage.tei_url = triage_data["tei_url"]
                    self.tei_url = triage_data["tei_url"]
                if "tei_model" in triage_data and should_apply("EMAIL_TRIAGE_TEI_MODEL"):
                    self.triage.tei_model = triage_data["tei_model"]
                    self.tei_model = triage_data["tei_model"]
                if "tei_api_key" in triage_data and should_apply("EMAIL_TRIAGE_TEI_API_KEY"):
                    self.triage.tei_api_key = triage_data["tei_api_key"]
                    self.tei_api_key = triage_data["tei_api_key"]
                if "tei_router_enabled" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TEI_ROUTER_ENABLED"):
                    self.triage.tei_router_enabled = bool(triage_data["tei_router_enabled"])
                if "tei_noise_enabled" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TEI_NOISE_ENABLED"):
                    self.triage.tei_noise_enabled = bool(triage_data["tei_noise_enabled"])
                if "tei_signal_enabled" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TEI_SIGNAL_ENABLED"):
                    self.triage.tei_signal_enabled = bool(triage_data["tei_signal_enabled"])
                if "tei_noise_threshold" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TEI_NOISE_THRESHOLD"):
                    self.triage.tei_noise_threshold = float(triage_data["tei_noise_threshold"])
                if "tei_signal_threshold" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TEI_SIGNAL_THRESHOLD"):
                    self.triage.tei_signal_threshold = float(triage_data["tei_signal_threshold"])
                if "whitelist_vip_senders" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__WHITELIST_VIP_SENDERS"):
                    self.triage.whitelist_vip_senders = triage_data["whitelist_vip_senders"]
                if "whitelist_domains" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__WHITELIST_DOMAINS"):
                    self.triage.whitelist_domains = triage_data["whitelist_domains"]
                if "blacklist_keywords" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__BLACKLIST_KEYWORDS"):
                    self.triage.blacklist_keywords = triage_data["blacklist_keywords"]
                if "blacklist_senders" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__BLACKLIST_SENDERS"):
                    self.triage.blacklist_senders = triage_data["blacklist_senders"]
                    
                # Map Logging section
                logging_data = yaml_data.get("logging", {})
                if "level" in logging_data and should_apply("EMAIL_TRIAGE_LOG_LEVEL"):
                    self.log_level = logging_data["level"].upper()

                # Map Scheduler section
                scheduler_data = yaml_data.get("scheduler", {})
                if "enabled" in scheduler_data and should_apply("EMAIL_TRIAGE_SCHEDULER_ENABLED"):
                    self.scheduler.enabled = bool(scheduler_data["enabled"])
                if "interval" in scheduler_data and should_apply("EMAIL_TRIAGE_SCHEDULER_INTERVAL"):
                    self.scheduler.interval = str(scheduler_data["interval"])
                if "max_per_account" in scheduler_data and should_apply("EMAIL_TRIAGE_SCHEDULER_MAX_PER_ACCOUNT"):
                    self.scheduler.max_per_account = int(scheduler_data["max_per_account"])
                if "days" in scheduler_data and should_apply("EMAIL_TRIAGE_SCHEDULER_DAYS"):
                    self.scheduler.days = int(scheduler_data["days"])

                # Map Download-All Scheduler section
                download_all_scheduler_data = yaml_data.get("download_all_scheduler", {})
                if "enabled" in download_all_scheduler_data and should_apply("EMAIL_TRIAGE_DOWNLOAD_ALL_SCHEDULER_ENABLED"):
                    self.download_all_scheduler.enabled = bool(download_all_scheduler_data["enabled"])
                if "interval" in download_all_scheduler_data and should_apply("EMAIL_TRIAGE_DOWNLOAD_ALL_SCHEDULER_INTERVAL"):
                    self.download_all_scheduler.interval = str(download_all_scheduler_data["interval"])

                # Map Auto Mark Read section -- each triage level configured independently
                auto_mark_read_data = yaml_data.get("auto_mark_read", {})
                for lvl in (0, 1, 2):
                    level_data = auto_mark_read_data.get(f"level_{lvl}", {})
                    level_settings = getattr(self.auto_mark_read, f"level_{lvl}")
                    if "enabled" in level_data and should_apply(f"EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_{lvl}_ENABLED"):
                        level_settings.enabled = bool(level_data["enabled"])
                    if "after_displays" in level_data and should_apply(
                        f"EMAIL_TRIAGE_AUTO_MARK_READ_LEVEL_{lvl}_AFTER_DISPLAYS"
                    ):
                        level_settings.after_displays = int(level_data["after_displays"])

                # Map Quality Check section
                quality_check_data = yaml_data.get("quality_check", {})
                if "enabled" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_ENABLED"):
                    self.quality_check.enabled = bool(quality_check_data["enabled"])
                if "hour" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_HOUR"):
                    self.quality_check.hour = int(quality_check_data["hour"])
                if "minute" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_MINUTE"):
                    self.quality_check.minute = int(quality_check_data["minute"])
                if "sample_rate" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_SAMPLE_RATE"):
                    self.quality_check.sample_rate = float(quality_check_data["sample_rate"])
                if "judge_base_url" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_JUDGE_BASE_URL"):
                    self.quality_check.judge_base_url = quality_check_data["judge_base_url"]
                if "judge_model" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_JUDGE_MODEL"):
                    self.quality_check.judge_model = quality_check_data["judge_model"]
                if "judge_api_key" in quality_check_data and should_apply("EMAIL_TRIAGE_QUALITY_CHECK_JUDGE_API_KEY"):
                    self.quality_check.judge_api_key = quality_check_data["judge_api_key"]

            except Exception as e:
                # Fallback gracefully to default initialization strings on error
                pass

    @classmethod
    def _load_for_profile_legacy(cls, profile_name: str = "default") -> "Settings":
        """The original filesystem-only resolution (profile name == directory name).

        Kept verbatim as a fallback for profiles that haven't been migrated into
        data/app.db yet -- see load_for_user for the DB-aware entry point.
        """
        workspace_root = Path(__file__).parent.resolve()

        if not profile_name:
            profile_name = "default"

        profile_dir = workspace_root / "profiles" / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Determine env file priority (profile env overrides root env)
        profile_env = profile_dir / ".env"
        env_file = profile_env if profile_env.exists() else workspace_root / ".env"

        s = cls(_env_file=env_file)
        s.workspace_dir = profile_dir
        s.gmail_token_path = profile_dir / "token.json"

        # Load global config first (inheritance base)
        s.load_from_yaml(workspace_root / "data" / "config.yml", env_file=env_file)

        # Overwrite with global local config if it exists
        global_local_yaml = workspace_root / "profiles" / "config-local.yml"
        if global_local_yaml.exists():
            s.load_from_yaml(global_local_yaml, env_file=env_file)

        # Overwrite with profile-specific config if it exists
        profile_yaml = profile_dir / "config.yml"
        if profile_yaml.exists():
            s.load_from_yaml(profile_yaml, env_file=env_file)

        # Overwrite with profile-specific local config if it exists
        profile_local_yaml = profile_dir / "config-local.yml"
        if profile_local_yaml.exists():
            s.load_from_yaml(profile_local_yaml, env_file=env_file)

        # Resolve relative credentials path to the profile directory if it exists there
        if s.gmail_credentials_path and not s.gmail_credentials_path.is_absolute():
            if (profile_dir / s.gmail_credentials_path).exists():
                s.gmail_credentials_path = profile_dir / s.gmail_credentials_path

        return s

    @classmethod
    def _load_for_user_row(cls, conn, user_row) -> "Settings":
        """Build Settings for a resolved data/app.db users row: same .env/YAML
        cascade as the legacy per-profile path (workspace_dir keyed by the
        user's immutable workspace_slug instead of a raw profile name), plus a
        final DB overlay (app_settings, then this user's user_settings) that
        wins over both YAML and env vars."""
        workspace_root = Path(__file__).parent.resolve()
        profile_dir = workspace_root / "profiles" / user_row["workspace_slug"]
        profile_dir.mkdir(parents=True, exist_ok=True)

        profile_env = profile_dir / ".env"
        env_file = profile_env if profile_env.exists() else workspace_root / ".env"

        s = cls(_env_file=env_file)
        s.workspace_dir = profile_dir
        s.gmail_token_path = profile_dir / "token.json"

        s.load_from_yaml(workspace_root / "data" / "config.yml", env_file=env_file)
        global_local_yaml = workspace_root / "profiles" / "config-local.yml"
        if global_local_yaml.exists():
            s.load_from_yaml(global_local_yaml, env_file=env_file)
        profile_yaml = profile_dir / "config.yml"
        if profile_yaml.exists():
            s.load_from_yaml(profile_yaml, env_file=env_file)
        profile_local_yaml = profile_dir / "config-local.yml"
        if profile_local_yaml.exists():
            s.load_from_yaml(profile_local_yaml, env_file=env_file)

        if s.gmail_credentials_path and not s.gmail_credentials_path.is_absolute():
            if (profile_dir / s.gmail_credentials_path).exists():
                s.gmail_credentials_path = profile_dir / s.gmail_credentials_path

        try:
            import app_settings_store
            app_settings_store.apply_to_settings(conn, s, user_id=user_row["id"])
        except Exception:
            pass

        return s

    @classmethod
    def _load_global(cls, conn=None) -> "Settings":
        """No-tenant view: env + data/config.yml + the global local override,
        then the global app_settings overlay. No filesystem side effect (no
        profile directory is created) -- used by the scheduler's process-wide
        reads and the module-level singleton."""
        workspace_root = Path(__file__).parent.resolve()
        env_file = workspace_root / ".env"

        s = cls(_env_file=env_file)
        s.workspace_dir = workspace_root
        s.load_from_yaml(workspace_root / "data" / "config.yml", env_file=env_file)
        global_local_yaml = workspace_root / "profiles" / "config-local.yml"
        if global_local_yaml.exists():
            s.load_from_yaml(global_local_yaml, env_file=env_file)

        owns_conn = False
        try:
            if conn is None:
                import appdb
                if appdb.DEFAULT_APP_DB_PATH.exists():
                    conn = appdb.connect()
                    owns_conn = True
            if conn is not None:
                import app_settings_store
                app_settings_store.apply_to_settings(conn, s, user_id=None)
        except Exception:
            pass
        finally:
            if owns_conn and conn is not None:
                conn.close()

        return s

    @classmethod
    def load_for_user(cls, user: "int | str | None" = None, *, conn=None) -> "Settings":
        """Resolve settings for a user (by data/app.db id, or username, or
        None for the global/no-tenant view).

        DB-backed users take the new path (workspace keyed by workspace_slug,
        DB settings overlay). A string that doesn't resolve to any DB user
        falls back to the legacy filesystem-only profile resolution, so
        pre-migration profile directories (and every caller still spelling
        `load_for_profile("jenny")`) keep working with zero changes until
        migrate_to_db.py has run.
        """
        if user is None:
            return cls._load_global(conn)

        resolved_conn = conn
        owns_conn = False
        user_row = None
        try:
            if resolved_conn is None:
                import appdb
                if appdb.DEFAULT_APP_DB_PATH.exists():
                    resolved_conn = appdb.connect()
                    owns_conn = True
            if resolved_conn is not None:
                import users_store
                user_row = users_store.resolve_user(resolved_conn, user)
        except Exception:
            user_row = None

        try:
            if user_row is not None:
                return cls._load_for_user_row(resolved_conn, user_row)
            return cls._load_for_profile_legacy(str(user))
        finally:
            if owns_conn and resolved_conn is not None:
                resolved_conn.close()

    @classmethod
    def load_for_profile(cls, profile_name: str = "default") -> "Settings":
        """Deprecated alias for load_for_user -- kept for auto_rater_*.py,
        classifier_tester.py, and any other caller still spelling this name."""
        return cls.load_for_user(profile_name)

settings = Settings.load_for_profile("default")

