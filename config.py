import os
import yaml
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class TriageSettings(BaseModel):
    confidence_threshold: float = 0.8
    triage_type: str = "llm"
    tei_url: str = "http://10.100.0.50:8077/predict"
    tei_router_enabled: bool = False
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

    triage: TriageSettings = Field(default_factory=TriageSettings)

    triage_base_url: str = "https://your-llm-proxy.com/v1"
    summary_base_url: str = "https://your-llm-proxy.com/v1"
    triage_model: str = "deepseek/deepseek-v4-flash"
    summary_model: str = "deepseek/deepseek-v4-pro"
    log_level: str = "INFO"
    tei_url: str = "http://10.100.0.50:8077/predict"

    # MCP Server settings
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # API Secret Keys kept strictly in environment context
    gemini_api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    triage_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_TRIAGE_API_KEY", os.getenv("EMAIL_TRIAGE_LLM_API_KEY", "")))
    summary_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_SUMMARY_API_KEY", os.getenv("EMAIL_TRIAGE_LLM_API_KEY", "")))

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
                if "tei_router_enabled" in triage_data and should_apply("EMAIL_TRIAGE_TRIAGE__TEI_ROUTER_ENABLED"):
                    self.triage.tei_router_enabled = bool(triage_data["tei_router_enabled"])
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
                    
            except Exception as e:
                # Fallback gracefully to default initialization strings on error
                pass

    @classmethod
    def load_for_profile(cls, profile_name: str = "default") -> "Settings":
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
        s.load_from_yaml(workspace_root / "config.yml", env_file=env_file)
        
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
            
        return s

settings = Settings.load_for_profile("default")

