import logging
import re
import json
from typing import Optional, Tuple
from pydantic import BaseModel, Field
import httpx
import tiktoken
from config import settings
from db import EmailDB

logger = logging.getLogger("email_triage.pipeline")

class TriageDecision(BaseModel):
    is_important: bool
    reason: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")

class SummaryResult(BaseModel):
    summary: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")

class EmailTriageEngine:
    def __init__(self, db: EmailDB) -> None:
        self.db = db
        # Set up the proxy endpoint URL and api key
        self.base_url = settings.llm_base_url.rstrip('/')
        self.api_key = settings.llm_api_key
        
        # Set up a reusable httpx Client with standard authorization headers
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        self.http_client = httpx.Client(timeout=45.0)
        
        try:
            self.encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            self.encoder = None

    def _estimate_tokens(self, text: str) -> int:
        if self.encoder:
            return len(self.encoder.encode(text))
        return len(text) // 4

    def run_level_0_static(self, sender: str, subject: str) -> Tuple[bool, Optional[str]]:
        """
        Level 0 Triage: Static noise filter via regex keywords.
        Returns (is_noise, reason).
        """
        combined_text = f"{sender} {subject}".lower()
        
        for kw in settings.triage.blacklist_keywords:
            if kw.lower() in combined_text:
                reason = f"Static filter hit: noise keyword '{kw}' matched"
                logger.info("Level 0 Filter hit: Found noise keyword '%s' in email.", kw)
                return True, reason
                
        for pattern in settings.triage.blacklist_senders:
            if re.search(re.escape(pattern.lower()), sender.lower()):
                reason = f"Static filter hit: sender pattern '{pattern}' matched"
                logger.info("Level 0 Filter hit: Sender matches blacklisted pattern '%s'.", pattern)
                return True, reason
                
        return False, None

    def run_level_1_classification(self, sender: str, subject: str, snippet: str) -> Tuple[bool, str, float]:
        """
        Level 1 Triage: LiteLLM / DeepSeek flash binary classification with JSON validation.
        """
        prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
        system_instruction = (
            "You are an expert executive assistant. Filter out automated updates, social notifications, "
            "promotions, and newsletters. Mark as important only specific human conversations, business critical alerts, "
            "or explicit requests directed to the recipient. You MUST return a valid JSON object containing exactly three fields: "
            "'is_important' (boolean), 'reason' (string), and 'confidence_score' (float from 0.0 to 1.0)."
        )
        
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": settings.triage_model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }
        
        try:
            logger.info("Level 1 Triage request sent to custom LiteLLM proxy model: %s", settings.triage_model)
            response = self.http_client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            
            resp_json = response.json()
            
            # Extract usage data from response if provided by proxy
            usage = resp_json.get("usage", {})
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 40)
            self.db.log_token_usage("level_1_classification", settings.triage_model, tokens_used)
            
            # Parse inner completion content
            content = resp_json["choices"][0]["message"]["content"]
            result_dict = json.loads(content)
            
            # Validate dictionary format via Pydantic
            result = TriageDecision.model_validate(result_dict)
            logger.info("Level 1 LiteLLM result for '%s': Important=%s (Reason: %s, Score: %s)", subject, result.is_important, result.reason, result.confidence_score)
            return result.is_important, result.reason, result.confidence_score
            
        except Exception as e:
            logger.error("Level 1 LiteLLM proxy classification failed: %s. Defaulting to True for safety.", e)
            return True, f"Proxy error: {e}", 1.0

    def run_level_2_summarization(self, subject: str, full_body: str) -> Tuple[str, float]:
        """
        Level 2 Summarization: DeepSeek pro high-quality bulleted executive summaries.
        """
        if not full_body or len(full_body.strip()) < 10:
            return "No substantive content to summarize.", 0.0

        prompt = f"Subject: {subject}\nBody:\n{full_body[:8000]}"
        system_instruction = (
            "Create clear, precise bulleted executive summaries. Be brief and highlight any requested task, conclusion, or deadline. "
            "You MUST return a valid JSON object containing exactly two fields: 'summary' (string) and 'confidence_score' (float from 0.0 to 1.0)."
        )
        
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": settings.summary_model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }
        
        try:
            logger.info("Level 2 Triage summary request sent to custom LiteLLM proxy model: %s", settings.summary_model)
            response = self.http_client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            
            resp_json = response.json()
            
            usage = resp_json.get("usage", {})
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 180)
            self.db.log_token_usage("level_2_summary", settings.summary_model, tokens_used)
            
            content = resp_json["choices"][0]["message"]["content"].strip()
            result_dict = json.loads(content)
            
            result = SummaryResult.model_validate(result_dict)
            logger.info("Level 2 summary successfully generated for '%s' (Score: %s)", subject, result.confidence_score)
            return result.summary, result.confidence_score
        except Exception as e:
            logger.error("Level 2 LiteLLM summarization failed: %s", e)
            return f"Failed to generate proxy summary due to error: {e}", 1.0
