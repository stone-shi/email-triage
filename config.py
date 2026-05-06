import os
import yaml
from pathlib import Path
from typing import List
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class TriageSettings(BaseModel):
    confidence_threshold: float = 0.8
    triage_type: str = "llm"
    tei_url: str = "http://10.100.0.50:8077/predict"
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

    llm_base_url: str = "https://your-llm-proxy.com/v1"
    triage_model: str = "deepseek/deepseek-v4-flash"
    summary_model: str = "deepseek/deepseek-v4-pro"
    log_level: str = "INFO"

    # API Secret Keys kept strictly in environment context
    gemini_api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    llm_api_key: str = Field(default_factory=lambda: os.getenv("EMAIL_TRIAGE_LLM_API_KEY", ""))

    def load_from_yaml(self) -> None:
        yaml_path = self.workspace_dir / "config.yml"
        if yaml_path.exists():
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    yaml_data = yaml.safe_load(f) or {}
                
                # Map LLM section
                llm_data = yaml_data.get("llm", {})
                if "base_url" in llm_data: self.llm_base_url = llm_data["base_url"]
                if "triage_model" in llm_data: self.triage_model = llm_data["triage_model"]
                if "summary_model" in llm_data: self.summary_model = llm_data["summary_model"]
                
                # Map Triage section
                triage_data = yaml_data.get("triage", {})
                if "confidence_threshold" in triage_data:
                    self.triage.confidence_threshold = float(triage_data["confidence_threshold"])
                if "triage_type" in triage_data:
                    self.triage.triage_type = triage_data["triage_type"]
                if "tei_url" in triage_data:
                    self.triage.tei_url = triage_data["tei_url"]
                if "whitelist_domains" in triage_data:
                    self.triage.whitelist_domains = triage_data["whitelist_domains"]
                if "blacklist_keywords" in triage_data:
                    self.triage.blacklist_keywords = triage_data["blacklist_keywords"]
                if "blacklist_senders" in triage_data:
                    self.triage.blacklist_senders = triage_data["blacklist_senders"]
                    
                # Map Logging section
                logging_data = yaml_data.get("logging", {})
                if "level" in logging_data:
                    self.log_level = logging_data["level"].upper()
                    
            except Exception as e:
                # Fallback gracefully to default initialization strings on error
                pass

settings = Settings()
# Execute YAML load immediately at module compilation startup phase
settings.load_from_yaml()
