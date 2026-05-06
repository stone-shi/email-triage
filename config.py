import os
import yaml
from pathlib import Path
from typing import List
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class TriageSettings(BaseModel):
    confidence_threshold: float = 0.8
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
    imap_password: str = "your_app_password_here"

    triage: TriageSettings = Field(default_factory=TriageSettings)

    llm_base_url: str = "https://your-llm-proxy.com/v1"
    triage_model: str = "deepseek/deepseek-v4-flash"
    summary_model: str = "deepseek/deepseek-v4-pro"

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
                
                # Map Gmail section
                gmail_data = yaml_data.get("gmail", {})
                if "credentials_path" in gmail_data: self.gmail_credentials_path = Path(gmail_data["credentials_path"])
                if "token_path" in gmail_data: self.gmail_token_path = Path(gmail_data["token_path"])
                if "account" in gmail_data: self.gmail_account = gmail_data["account"]
                
                # Map IMAP section
                imap_data = yaml_data.get("imap", {})
                if "host" in imap_data: self.imap_host = imap_data["host"]
                if "port" in imap_data: self.imap_port = int(imap_data["port"])
                if "login" in imap_data: self.imap_login = imap_data["login"]
                if "password" in imap_data: self.imap_password = imap_data["password"]
                
                # Map Triage section
                triage_data = yaml_data.get("triage", {})
                if "confidence_threshold" in triage_data:
                    self.triage.confidence_threshold = float(triage_data["confidence_threshold"])
                if "whitelist_domains" in triage_data:
                    self.triage.whitelist_domains = triage_data["whitelist_domains"]
                if "blacklist_keywords" in triage_data:
                    self.triage.blacklist_keywords = triage_data["blacklist_keywords"]
                if "blacklist_senders" in triage_data:
                    self.triage.blacklist_senders = triage_data["blacklist_senders"]
                    
            except Exception as e:
                # Fallback gracefully to default initialization strings on error
                pass

settings = Settings()
# Execute YAML load immediately at module compilation startup phase
settings.load_from_yaml()
