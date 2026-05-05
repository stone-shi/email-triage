import os
from pathlib import Path
from typing import List
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class TriageSettings(BaseModel):
    # Level 0 Static Blacklist Keywords (Case-insensitive)
    blacklist_keywords: List[str] = [
        "unsubscribe", "newsletter", "promotions", "marketing", 
        "no-reply", "noreply", "digest", "advertisement"
    ]
    # Level 0 Blacklisted Senders
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
    
    # Gmail OAuth paths
    gmail_credentials_path: Path = Path("credentials.json")
    gmail_token_path: Path = Field(default_factory=lambda: Path(__file__).parent.resolve() / "token.json")
    gmail_account: str = "your_email@gmail.com"
    headless_mode: bool = False

    # Flat IMAP config properties driven from environment
    imap_host: str = "imap.zoho.com"
    imap_port: int = 993
    imap_login: str = "your_email@domain.com"
    imap_password: str = "your_app_password_here"

    # Triage rules
    triage: TriageSettings = Field(default_factory=TriageSettings)

    # LLM Configuration
    gemini_api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    llm_base_url: str = ""
    llm_api_key: str = ""
    triage_model: str = ""
    summary_model: str = ""

settings = Settings()
