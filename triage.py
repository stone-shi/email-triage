import logging
import re
import json
import time
from typing import Optional, Tuple, Dict, Any
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
    tag: str = Field(default="notification", description="One word classification tag (e.g., promotion, notification, personal, vip)")

class SummaryResult(BaseModel):
    summary: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")
    tag: str = Field(default="vip", description="One word classification tag (e.g., promotion, notification, personal, vip)")

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
        self.http_client = httpx.Client(timeout=1800.0)
        
        # Load external prompts if present
        import yaml
        prompts_path = settings.workspace_dir / "prompts.yml"
        try:
            if prompts_path.exists():
                with open(prompts_path, "r", encoding="utf-8") as f:
                    self.prompts = yaml.safe_load(f) or {}
            else:
                self.prompts = {}
        except Exception:
            self.prompts = {}
            
        try:
            self.encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            self.encoder = None

    def _estimate_tokens(self, text: str) -> int:
        if self.encoder:
            return len(self.encoder.encode(text))
        return len(text) // 4

    def is_vip_sender(self, sender: str) -> bool:
        """Checks if the sender matches any entry in the VIP whitelist."""
        for vip in getattr(settings.triage, "whitelist_vip_senders", []):
            if vip.lower() in sender.lower():
                return True
        return False

    def run_level_0_static(self, sender: str, subject: str) -> Tuple[bool, Optional[str]]:
        """
        Level 0 Triage: Static noise filter via regex keywords.
        Returns (is_noise, reason).
        """
        combined_text = f"{sender} {subject}".lower()
        
        for domain in getattr(settings.triage, "whitelist_domains", []):
            if domain.lower() in sender.lower():
                logger.info("Level 0 Whitelist hit: Sender domain '%s' is whitelisted. Bypassing noise filter.", domain)
                return False, None
        
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

    def _extract_json(self, text: str) -> str:
        """
        Extracts JSON content from text, stripping markdown code blocks if present.
        """
        text = text.strip()
        if text.startswith("```"):
            # Match ```json ... ``` or just ``` ... ```
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if match:
                return match.group(1).strip()
        return text

    def run_level_1_classification(self, sender: str, subject: str, snippet: str, model_name: Optional[str] = None) -> Tuple[bool, str, float, str, Dict[str, Any]]:
        """
        Level 1 Triage: LiteLLM / DeepSeek flash binary classification with JSON validation.
        Returns (is_important, reason, score, tag, metrics).
        """
        if not model_name:
            model_name = settings.triage_model
            
        prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
        
        metrics = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_sec": 0.0
        }
        
        start_time = time.time()
        
        # TEI Classifier Ingestion Pathway Switch
        if getattr(settings.triage, "triage_type", "llm") == "tei":
            tei_text = f"From: {sender} | Subject: {subject} | Snippet: {snippet}"
            try:
                logger.info("Level 1 Triage request sent to TEI Sequence Classifier server: %s", settings.triage.tei_url)
                response = self.http_client.post(settings.triage.tei_url, json={"inputs": tei_text})
                response.raise_for_status()
                predictions = response.json()
                
                winning_pred = max(predictions, key=lambda x: x.get("score", 0.0))
                winning_label = winning_pred.get("label", "").lower()
                winning_score = winning_pred.get("score", 1.0)
                
                is_important = ("entailment" in winning_label and "not_" not in winning_label) or "important" in winning_label
                reason = f"TEI Classifier resolved winning label: '{winning_label}'"
                tag = "notification" if not is_important else "personal"
                
                logger.info("Level 1 TEI Classifier result for '%s': Important=%s (Score: %s)", subject, is_important, winning_score)
                metrics["duration_sec"] = time.time() - start_time
                return is_important, reason, winning_score, tag, metrics
            except Exception as tei_err:
                logger.error("Level 1 TEI Classifier server prediction failed: %s. Falling back to safety True.", tei_err)
                metrics["duration_sec"] = time.time() - start_time
                return True, f"TEI server prediction error: {tei_err}", 1.0, "personal", metrics
                
        system_instruction = self.prompts.get("level_1_fast_triage", {}).get("system")
        if not system_instruction:
            system_instruction = (
                "You are an expert executive assistant. Filter out automated updates, social notifications, "
                "promotions, and newsletters. Mark as important only specific human conversations, business critical alerts, "
                "or explicit requests directed to the recipient. You MUST return a valid JSON object containing exactly four fields: "
                "'is_important' (boolean), 'reason' (string), 'confidence_score' (float from 0.0 to 1.0), and "
                "'tag' (a one word lowercase tag, e.g., promotion, notification, personal, vip)."
            )
        
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "include_reasoning": False,
        }
        
        try:
            logger.info("Level 1 Triage request sent to custom LiteLLM proxy model: %s", model_name)
            response = self.http_client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            
            try:
                resp_json = response.json()
            except json.JSONDecodeError as e:
                logger.error("Level 1 Proxy response is not valid JSON. Status: %s, Body: %s", response.status_code, response.text)
                raise e
            
            # Extract usage data from response if provided by proxy
            usage = resp_json.get("usage", {})
            metrics["prompt_tokens"] = usage.get("prompt_tokens", 0)
            metrics["completion_tokens"] = usage.get("completion_tokens", 0)
            
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 40)
            self.db.log_token_usage("level_1_classification", model_name, tokens_used)
            
            # Parse inner completion content
            content = resp_json["choices"][0]["message"]["content"]
            if not content:
                logger.error("Level 1 LLM returned empty content. Response: %s", resp_json)
                raise ValueError("Empty content from LLM")

            json_content = self._extract_json(content)
            try:
                result_dict = json.loads(json_content)
            except json.JSONDecodeError as e:
                logger.error("Level 1 failed to parse inner JSON content: %s", json_content)
                raise e
            
            # Validate dictionary format via Pydantic
            result = TriageDecision.model_validate(result_dict)
            logger.info("Level 1 LiteLLM result for '%s': Important=%s (Reason: %s, Score: %s, Tag: %s)", subject, result.is_important, result.reason, result.confidence_score, result.tag)
            
            metrics["duration_sec"] = time.time() - start_time
            return result.is_important, result.reason, result.confidence_score, result.tag, metrics
            
        except Exception as e:
            logger.error("Level 1 LiteLLM proxy classification failed: %s. Defaulting to True for safety.", e)
            if 'content' in locals():
                logger.error("Raw unparsed Level 1 response text was: \n%s", content)
            elif 'response' in locals():
                logger.error("Raw proxy server response status body text was: \n%s", response.text)
                
            metrics["duration_sec"] = time.time() - start_time
            return True, f"Proxy error: {e}", 1.0, "personal", metrics

    def run_level_2_summarization(self, subject: str, full_body: str, model_name: Optional[str] = None) -> Tuple[str, float, str, Dict[str, Any]]:
        """
        Level 2 Summarization: DeepSeek pro high-quality bulleted executive summaries.
        Returns (summary, score, tag, metrics).
        """
        if not model_name:
            model_name = settings.summary_model
            
        metrics = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_sec": 0.0
        }
        
        if not full_body or len(full_body.strip()) < 10:
            return "No substantive content to summarize.", 0.0, "notification", metrics

        prompt = f"Subject: {subject}\nBody:\n{full_body[:8000]}"
        system_instruction = self.prompts.get("level_2_summarization", {}).get("system")
        if not system_instruction:
            system_instruction = (
                "Create clear, precise bulleted executive summaries. Be brief and highlight any requested task, conclusion, or deadline. "
                "You MUST return a valid JSON object containing exactly three fields: 'summary' (string), 'confidence_score' (float from 0.0 to 1.0), "
                "and 'tag' (a one word lowercase tag, e.g., personal, vip, update)."
            )
        
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "include_reasoning": False,
        }
        
        start_time = time.time()
        try:
            logger.info("Level 2 Triage summary request sent to custom LiteLLM proxy model: %s", model_name)
            response = self.http_client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            
            try:
                resp_json = response.json()
            except json.JSONDecodeError as e:
                logger.error("Level 2 Proxy response is not valid JSON. Status: %s, Body: %s", response.status_code, response.text)
                raise e
            
            usage = resp_json.get("usage", {})
            metrics["prompt_tokens"] = usage.get("prompt_tokens", 0)
            metrics["completion_tokens"] = usage.get("completion_tokens", 0)
            
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 180)
            self.db.log_token_usage("level_2_summary", model_name, tokens_used)
            
            content = resp_json["choices"][0]["message"]["content"].strip()
            if not content:
                logger.error("Level 2 LLM returned empty content. Response: %s", resp_json)
                raise ValueError("Empty content from LLM")

            json_content = self._extract_json(content)
            try:
                result_dict = json.loads(json_content)
            except json.JSONDecodeError as e:
                logger.error("Level 2 failed to parse inner JSON content: %s", json_content)
                raise e
            
            result = SummaryResult.model_validate(result_dict)
            logger.info("Level 2 summary successfully generated for '%s' (Score: %s, Tag: %s)", subject, result.confidence_score, result.tag)
            
            metrics["duration_sec"] = time.time() - start_time
            return result.summary, result.confidence_score, result.tag, metrics
        except Exception as e:
            logger.error("Level 2 LiteLLM summarization failed: %s", e)
            if 'content' in locals():
                logger.error("Raw unparsed Level 2 response text was: \n%s", content)
            elif 'response' in locals():
                logger.error("Raw proxy server response body text was: \n%s", response.text)
                
            metrics["duration_sec"] = time.time() - start_time
            return f"Failed to generate proxy summary due to error: {e}", 1.0, "vip", metrics

    def run_level_1_premium_escalation(self, sender: str, subject: str, snippet: str, full_body: str) -> Tuple[bool, str, float, str]:
        """
        Secondary Premium Triage Escalation layer: Uses the premium summary model and full text body 
        to re-evaluate borderline/ambiguous classification choices definitively.
        """
        prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}\nFull Body Content:\n{full_body[:6000]}"
        system_instruction = self.prompts.get("level_1_premium_escalation", {}).get("system")
        if not system_instruction:
            system_instruction = (
                "You are a premium AI operations auditor resolving an ambiguous email priority classification query. "
                "Filter out automated noise, promotions, and notifications. Mark as important only actionable, high-priority human "
                "conversations, business critical text streams, or direct requests requiring attention.\n"
                "You MUST return a valid JSON object containing exactly four fields: "
                "'is_important' (boolean), 'reason' (string), 'confidence_score' (float from 0.0 to 1.0), and "
                "'tag' (a one word lowercase tag, e.g., personal, vip, promotion, notification)."
            )
        
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": settings.summary_model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "include_reasoning": False,
        }
        
        try:
            logger.info("Ambiguity Triage Escalation sent to premium model: %s", settings.summary_model)
            response = self.http_client.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            
            resp_json = response.json()
            usage = resp_json.get("usage", {})
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 40)
            self.db.log_token_usage("premium_triage_escalation", settings.summary_model, tokens_used)
            
            content = resp_json["choices"][0]["message"]["content"]
            json_content = self._extract_json(content)
            result_dict = json.loads(json_content)
            
            result = TriageDecision.model_validate(result_dict)
            logger.info("Premium Escalation result for '%s': Important=%s (Reason: %s, Score: %s, Tag: %s)", subject, result.is_important, result.reason, result.confidence_score, result.tag)
            return result.is_important, result.reason, result.confidence_score, result.tag
            
        except Exception as e:
            logger.error("Premium triage escalation failed: %s. Safely returning True.", e)
            if 'content' in locals():
                logger.error("Raw unparsed premium escalation response text was: \n%s", content)
            elif 'response' in locals():
                logger.error("Raw proxy server response body text was: \n%s", response.text)
            return True, f"Escalation error: {e}", 1.0, "personal"
