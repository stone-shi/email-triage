import logging
import math
import re
import json
import time
from typing import Optional, Tuple, Dict, Any, List
from pydantic import BaseModel, Field
import httpx
import tiktoken
from config import settings
from db import EmailDB
import prompts_store

logger = logging.getLogger("email_triage.pipeline")

# Anchor documents reranked against each email to derive an importance/noise signal
RERANK_IMPORTANT_ANCHOR = "An urgent personal message from a specific person requiring your direct reply, decision, or action, such as a work request, deadline, bill, or critical account issue."
RERANK_NOISE_ANCHOR = "An automated system notification, media download alert, promotional marketing email, newsletter, or subscription update that does not require any reply or action from you."

# Upper bound on completion tokens per LLM stage. These are runaway guardrails, not tight
# budgets: reasoning models spend most of their completion budget on hidden thinking tokens
# before emitting the small JSON payload we actually parse, and not every backend honors
# `include_reasoning: false` (some local ones stream `reasoning` deltas regardless). Without
# a ceiling, such a model can spend thousands of tokens -- minutes of wall clock -- deciding
# a single Level 1 classification. Keep these generous enough that a thinking model still
# reaches its JSON answer, since a completion truncated mid-thought comes back with empty
# content and fails the parse (some proxies turn that into a 502) rather than degrading.
MAX_TOKENS_LEVEL_1 = 3072
MAX_TOKENS_PREMIUM_ESCALATION = 3072
MAX_TOKENS_LEVEL_2 = 4096


def _sigmoid(x: float) -> float:
    """Numerically-stable sigmoid, used to map a raw cross-encoder logit into (0,1)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)

class TriageDecision(BaseModel):
    suggested_level: int = Field(description="Suggested triage level: 0 (noise), 1 (notification/promotion), 2 (important)")
    reason: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")
    tag: str = Field(default="notification", description="One word classification tag (e.g., promotion, notification, personal, vip)")

class SummaryResult(BaseModel):
    summary: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")
    tag: str = Field(default="vip", description="One word classification tag (e.g., promotion, notification, personal, vip)")

class EmailTriageEngine:
    def __init__(self, db: EmailDB, settings_instance: Optional[Any] = None) -> None:
        self.db = db
        self.settings = settings_instance if settings_instance else settings
        # Set up backward-compatible proxy endpoint URL and api key
        self.base_url = self.settings.llm_base_url.rstrip('/')
        self.api_key = self.settings.llm_api_key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        # Decoupled endpoints and headers for triage and summary stages
        self.triage_base_url = self.settings.triage_base_url.rstrip('/')
        self.triage_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.triage_api_key}"
        }
        self.summary_base_url = self.settings.summary_base_url.rstrip('/')
        self.summary_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.summary_api_key}"
        }
        self.http_client = httpx.Client(timeout=1800.0)
        
        # Load external prompts if present
        import yaml
        prompts_path = self.settings.workspace_dir / "prompts.yml"
        try:
            if prompts_path.exists():
                with open(prompts_path, "r", encoding="utf-8") as f:
                    self.prompts = yaml.safe_load(f) or {}
            else:
                self.prompts = {}
        except Exception:
            self.prompts = {}

        # Global, admin-editable overrides from data/app.db win over prompts.yml,
        # which wins over the hardcoded defaults in each run_level_* method below --
        # same DB-vs-legacy precedence as every other global setting in this app.
        # No-op (falls through silently) until data/app.db exists and has been
        # seeded -- see prompts_store.seed_from_yaml_or_defaults, called once at
        # SSE server startup.
        try:
            import appdb

            if appdb.DEFAULT_APP_DB_PATH.exists():
                with appdb.get_conn() as conn:
                    for key, value in prompts_store.get_all_prompts_raw(conn).items():
                        self.prompts.setdefault(key, {})["system"] = value
        except Exception:
            logger.exception("Failed to load DB-backed prompt overrides; using prompts.yml/hardcoded defaults")


        try:
            self.encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            self.encoder = None

    def _estimate_tokens(self, text: str) -> int:
        if self.encoder:
            return len(self.encoder.encode(text))
        return len(text) // 4

    def _rerank(self, query: str, documents: List[str]) -> List[float]:
        """
        Calls the reranker's /rerank endpoint and returns relevance scores in the
        same order as `documents` (the API itself returns results sorted by score,
        so we re-index them by the `index` field to restore input order).

        Not every /rerank backend returns a calibrated [0,1] relevance_score the
        way Cohere/Jina's hosted APIs do -- a raw cross-encoder (e.g. a
        sentence-transformers CrossEncoder served as-is) instead returns an
        unbounded pre-sigmoid logit (can be well outside [0,1], negative for a
        poor match). Comparing that directly against tei_noise_threshold/
        confidence_threshold (both authored assuming a 0-1 scale) would be
        meaningless, so triage.tei_score_normalize (off by default, to avoid
        double-transforming a backend that's already calibrated) applies a
        sigmoid to bring raw logits back into (0,1) -- monotonic, so it never
        changes which of two scores is larger, only rescales the magnitude.
        """
        headers = {"Content-Type": "application/json"}
        if getattr(self.settings.triage, "tei_api_key", None):
            headers["Authorization"] = f"Bearer {self.settings.triage.tei_api_key}"

        payload = {
            "model": self.settings.triage.tei_model,
            "query": query,
            "documents": documents,
        }
        response = self.http_client.post(self.settings.triage.tei_url, headers=headers, json=payload)
        response.raise_for_status()
        results = response.json().get("results", [])

        scores = [0.0] * len(documents)
        for r in results:
            idx = r.get("index")
            if idx is not None and 0 <= idx < len(scores):
                scores[idx] = r.get("relevance_score", 0.0)

        if getattr(self.settings.triage, "tei_score_normalize", False):
            scores = [_sigmoid(s) for s in scores]
        return scores

    def is_vip_sender(self, sender: str) -> bool:
        """Checks if the sender matches any entry in the VIP whitelist."""
        for vip in getattr(self.settings.triage, "whitelist_vip_senders", []):
            if vip.lower() in sender.lower():
                return True
        return False

    def run_level_0_static(self, sender: str, subject: str) -> Tuple[bool, Optional[str]]:
        """
        Level 0 Triage: Static noise filter via regex keywords.
        Returns (is_noise, reason).
        """
        combined_text = f"{sender} {subject}".lower()
        
        for domain in getattr(self.settings.triage, "whitelist_domains", []):
            if domain.lower() in sender.lower():
                logger.info("Level 0 Whitelist hit: Sender domain '%s' is whitelisted. Bypassing noise filter.", domain)
                return False, None
        
        for kw in self.settings.triage.blacklist_keywords:
            if kw.lower() in combined_text:
                reason = f"Static filter hit: noise keyword '{kw}' matched"
                logger.info("Level 0 Filter hit: Found noise keyword '%s' in email.", kw)
                return True, reason
                
        for pattern in self.settings.triage.blacklist_senders:
            if re.search(re.escape(pattern.lower()), sender.lower()):
                reason = f"Static filter hit: sender pattern '{pattern}' matched"
                logger.info("Level 0 Filter hit: Sender matches blacklisted pattern '%s'.", pattern)
                return True, reason
                
        return False, None

    def run_rerank_router(self, sender: str, subject: str, snippet: str) -> Tuple[Optional[int], Optional[str], float]:
        """
        Level 0.5 rerank noise filter: a cheap, high-precision gate that runs before
        the Level 1 LLM call to skip obvious noise for free. Scores the email against
        a single "noise" anchor document via the reranker's /rerank endpoint
        (Cohere/Jina-style: model + query + documents). Deliberately one-directional --
        it only ever short-circuits to Level 0; anything not confidently noise falls
        through to Level 1 so it still gets a real classification (an express lane
        straight to Level 2 based on embedding similarity alone, with no LLM sanity
        check, cost more than it saved and risked mis-escalating on a single score).
        Returns (suggested_level_override, reason, confidence).
        """
        if not self.settings.triage.tei_router_enabled:
            return None, None, 1.0
        if not getattr(self.settings.triage, "tei_noise_enabled", True):
            return None, None, 1.0

        query_text = f"From: {sender} | Subject: {subject} | Snippet: {snippet}"
        try:
            logger.info("Level 0.5 rerank noise-filter request sent to server: %s", self.settings.triage.tei_url)
            (noise_score,) = self._rerank(query_text, [RERANK_NOISE_ANCHOR])

            if noise_score >= self.settings.triage.tei_noise_threshold:
                reason = f"Rerank noise filter: noise score {noise_score:.4f}"
                logger.info("Level 0.5 rerank filter: noise detected with score %s", noise_score)
                return 0, reason, noise_score

            return None, f"Rerank neutral: noise score {noise_score:.4f}", noise_score
        except Exception as e:
            logger.error("Level 0.5 rerank noise filter failed: %s", e)
            return None, None, 0.0

    def _extract_json(self, text: str) -> str:
        """
        Extracts JSON content from text, stripping markdown code blocks if present.
        Also attempts to fix common LLM formatting errors like unquoted string values.
        """
        text = text.strip()
        if text.startswith("```"):
            # Match ```json ... ``` or just ``` ... ```
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        
        # Robustness fix: some smaller models (like Qwen 0.8B) return unquoted tags: "tag": promotion
        # We look for "tag": followed by a single word that is NOT quoted and NOT a boolean/null
        text = re.sub(r'("tag":\s*)(?!(?:true|false|null)\b)([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*[,}])', r'\1"\2"', text)
        
        # Robustness fix: handle invalid escapes like \' which some models return
        text = text.replace("\\'", "'")
        
        return text

    def run_level_1_classification(self, sender: str, subject: str, snippet: str, model_name: Optional[str] = None) -> Tuple[int, str, float, str, Dict[str, Any]]:
        """
        Level 1 Triage: LiteLLM / DeepSeek flash ternary classification with JSON validation.
        Returns (suggested_level, reason, score, tag, metrics).
        """
        if not model_name:
            model_name = self.settings.triage_model
            
        prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}"
        
        metrics = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_sec": 0.0
        }
        
        start_time = time.time()
        
        # Rerank Classifier Ingestion Pathway Switch
        if getattr(self.settings.triage, "triage_type", "llm") == "tei":
            query_text = f"From: {sender} | Subject: {subject} | Snippet: {snippet}"
            try:
                logger.info("Level 1 Triage request sent to Rerank Classifier server: %s", self.settings.triage.tei_url)
                important_score, noise_score = self._rerank(query_text, [RERANK_IMPORTANT_ANCHOR, RERANK_NOISE_ANCHOR])

                is_important = important_score >= noise_score
                suggested_level = 2 if is_important else 1
                winning_score = important_score if is_important else noise_score
                reason = f"Rerank Classifier resolved importance={important_score:.4f} noise={noise_score:.4f}"
                tag = "personal" if is_important else "notification"

                logger.info("Level 1 Rerank Classifier result for '%s': SuggestedLevel=%s (Score: %s)", subject, suggested_level, winning_score)
                metrics["duration_sec"] = time.time() - start_time
                return suggested_level, reason, winning_score, tag, metrics
            except Exception as rerank_err:
                logger.error("Level 1 Rerank Classifier server prediction failed: %s. Falling back to safety Level 2.", rerank_err)
                metrics["duration_sec"] = time.time() - start_time
                return 2, f"Rerank server prediction error: {rerank_err}", 1.0, "personal", metrics
                
        system_instruction = self.prompts.get("level_1_fast_triage", {}).get("system")
        if not system_instruction:
            system_instruction = prompts_store.DEFAULT_PROMPTS["level_1_fast_triage"]
        
        url = f"{self.triage_base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "include_reasoning": False,
            "stream": False,
            "max_tokens": MAX_TOKENS_LEVEL_1,
        }

        try:
            logger.info("Level 1 Triage request sent to custom LiteLLM proxy model: %s", model_name)
            response = self.http_client.post(url, headers=self.triage_headers, json=payload)
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
            logger.info("Level 1 LiteLLM result for '%s': SuggestedLevel=%s (Reason: %s, Score: %s, Tag: %s)", subject, result.suggested_level, result.reason, result.confidence_score, result.tag)
            
            metrics["duration_sec"] = time.time() - start_time
            return result.suggested_level, result.reason, result.confidence_score, result.tag, metrics
            
        except Exception as e:
            logger.error("Level 1 LiteLLM proxy classification failed: %s. Defaulting to Level 2 for safety.", e)
            if 'content' in locals():
                logger.error("Raw unparsed Level 1 response text was: \n%s", content)
            elif 'response' in locals():
                logger.error("Raw proxy server response status body text was: \n%s", response.text)
                
            metrics["duration_sec"] = time.time() - start_time
            return 2, f"Proxy error: {e}", 1.0, "personal", metrics

    def run_level_2_summarization(self, subject: str, full_body: str, model_name: Optional[str] = None) -> Tuple[str, float, str, Dict[str, Any]]:
        """
        Level 2 Summarization: DeepSeek pro high-quality bulleted executive summaries.
        Returns (summary, score, tag, metrics).
        """
        if not model_name:
            model_name = self.settings.summary_model
            
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
            system_instruction = prompts_store.DEFAULT_PROMPTS["level_2_summarization"]
        
        url = f"{self.summary_base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "include_reasoning": False,
            "stream": False,
            "max_tokens": MAX_TOKENS_LEVEL_2,
        }

        start_time = time.time()
        try:
            logger.info("Level 2 Triage summary request sent to custom LiteLLM proxy model: %s", model_name)
            response = self.http_client.post(url, headers=self.summary_headers, json=payload)
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

    def run_level_1_premium_escalation(self, sender: str, subject: str, snippet: str, full_body: str) -> Tuple[int, str, float, str]:
        """
        Secondary Premium Triage Escalation layer: Uses the premium summary model and full text body 
        to re-evaluate borderline/ambiguous classification choices definitively.
        """
        prompt = f"Sender: {sender}\nSubject: {subject}\nSnippet: {snippet}\nFull Body Content:\n{full_body[:6000]}"
        system_instruction = self.prompts.get("level_1_premium_escalation", {}).get("system")
        if not system_instruction:
            system_instruction = prompts_store.DEFAULT_PROMPTS["level_1_premium_escalation"]
        
        url = f"{self.summary_base_url}/chat/completions"
        payload = {
            "model": self.settings.summary_model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "include_reasoning": False,
            "stream": False,
            "max_tokens": MAX_TOKENS_PREMIUM_ESCALATION,
        }

        try:
            logger.info("Ambiguity Triage Escalation sent to premium model: %s", self.settings.summary_model)
            response = self.http_client.post(url, headers=self.summary_headers, json=payload)
            response.raise_for_status()
            
            resp_json = response.json()
            usage = resp_json.get("usage", {})
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 40)
            self.db.log_token_usage("premium_triage_escalation", self.settings.summary_model, tokens_used)
            
            content = resp_json["choices"][0]["message"]["content"]
            json_content = self._extract_json(content)
            result_dict = json.loads(json_content)
            
            result = TriageDecision.model_validate(result_dict)
            logger.info("Premium Escalation result for '%s': SuggestedLevel=%s (Reason: %s, Score: %s, Tag: %s)", subject, result.suggested_level, result.reason, result.confidence_score, result.tag)
            return result.suggested_level, result.reason, result.confidence_score, result.tag
            
        except Exception as e:
            logger.error("Premium triage escalation failed: %s. Safely returning Level 2.", e)
            if 'content' in locals():
                logger.error("Raw unparsed premium escalation response text was: \n%s", content)
            elif 'response' in locals():
                logger.error("Raw proxy server response body text was: \n%s", response.text)
            return 2, f"Escalation error: {e}", 1.0, "personal"

    def _mark_account_read(
        self, client: Any, account_label: str, provider: str,
        level: Optional[int], message_id: Optional[str], all_emails: bool,
    ) -> Dict[str, Any]:
        """One account's worth of mark-emails-read: fetch its unread set, find
        matches, mark them remotely, reflect successful marks in the local
        cache immediately (so fetch_and_process_unread doesn't show stale
        unread items until the next background sync tick reconciles them)."""
        errors: List[str] = []
        try:
            unread = client.fetch_unread_messages() if provider == "gmail" else client.fetch_unread_headers()
        except Exception as e:
            logger.error("Failed to fetch %s unread messages during mark-read for %s: %s", provider, account_label, e)
            errors.append(f"{provider} fetch error: {e}")
            unread = []

        matching_ids: List[str] = []
        for e in unread:
            mid = e["message_id"]
            internal_id = e["id"]
            if all_emails:
                matching_ids.append(internal_id)
            elif message_id and (mid == message_id or internal_id == message_id):
                matching_ids.append(internal_id)
            elif level is not None:
                cached = self.db.get_cached_result(mid)
                if cached and cached.get("triage_level") == level:
                    matching_ids.append(internal_id)

        marked: List[str] = []
        if matching_ids:
            try:
                success = client.mark_as_read(matching_ids)
                if success:
                    marked = matching_ids
                else:
                    errors.append(f"Failed to execute {provider} mark-as-read")
            except Exception as e:
                errors.append(f"{provider} modify error: {e}")

        if marked:
            id_to_message_id = {e["id"]: e["message_id"] for e in unread}
            for internal_id in marked:
                mid = id_to_message_id.get(internal_id)
                if mid:
                    self.db.upsert_email_metadata(message_id=mid, account=account_label, is_unread=False)

        return {"account": account_label, "provider": provider, "marked": len(marked), "ids": marked, "errors": errors}

    def _resolve_mark_read_accounts(self) -> Optional[List[Any]]:
        """None means no data/app.db user exists for this profile -- the caller
        falls back to the original single-Gmail+single-IMAP construction."""
        try:
            import appdb
            import users_store
            import account_clients

            if not appdb.DEFAULT_APP_DB_PATH.exists():
                return None
            with appdb.get_conn() as conn:
                user_row = users_store.get_user_by_username(conn, self.settings.workspace_dir.name)
                if user_row is None:
                    return None
                return account_clients.clients_for_user(conn, user_row["id"], self.settings, for_triage=True)
        except Exception as e:
            logger.error("Failed to resolve DB-backed integrations for mark-read: %s", e)
            return None

    def mark_emails_read(
        self,
        level: Optional[int] = None,
        message_id: Optional[str] = None,
        all_emails: bool = False
    ) -> Dict[str, Any]:
        """
        Marks unread emails in the mailboxes as read based on criteria:
        - all_emails=True: mark all unread emails read.
        - message_id: mark the specific message with this Message-ID/internal-ID read.
        - level: mark all unread emails with this cached triage level (0, 1, or 2) read.

        Loops over however many Gmail/Zoho/IMAP accounts a data/app.db user has
        connected; falls back to the original single-Gmail+single-IMAP pair when
        no such user exists yet. gmail_marked_count/imap_marked_count/gmail_ids/
        imap_uids are derived sums/lists (first gmail-family and first imap/zoho-
        family account) kept for backward compatibility with existing callers.
        """
        from gmail_client import GmailClient
        from imap_client import IMAPClient

        accounts_out: List[Dict[str, Any]] = []
        db_accounts = self._resolve_mark_read_accounts()

        if db_accounts is not None:
            for ac in db_accounts:
                accounts_out.append(self._mark_account_read(ac.client, ac.account, ac.provider, level, message_id, all_emails))
        else:
            try:
                gmail = GmailClient(settings_instance=self.settings)
                accounts_out.append(
                    self._mark_account_read(gmail, self.settings.gmail_account, "gmail", level, message_id, all_emails)
                )
            except Exception as e:
                logger.error("Failed to construct Gmail client during mark-read: %s", e)
                accounts_out.append({
                    "account": self.settings.gmail_account, "provider": "gmail", "marked": 0, "ids": [],
                    "errors": [f"gmail fetch error: {e}"],
                })

            try:
                imap = IMAPClient(settings_instance=self.settings)
                accounts_out.append(
                    self._mark_account_read(imap, self.settings.imap_login, "imap", level, message_id, all_emails)
                )
            except Exception as e:
                logger.error("Failed to construct IMAP client during mark-read: %s", e)
                accounts_out.append({
                    "account": self.settings.imap_login, "provider": "imap", "marked": 0, "ids": [],
                    "errors": [f"imap fetch error: {e}"],
                })

        gmail_result = next((a for a in accounts_out if a["provider"] == "gmail"), None)
        imap_result = next((a for a in accounts_out if a["provider"] in ("imap", "zoho")), None)
        errors = [err for a in accounts_out for err in a["errors"]]

        return {
            "gmail_marked_count": gmail_result["marked"] if gmail_result else 0,
            "imap_marked_count": imap_result["marked"] if imap_result else 0,
            "gmail_ids": gmail_result["ids"] if gmail_result else [],
            "imap_uids": imap_result["ids"] if imap_result else [],
            "accounts": accounts_out,
            "errors": errors,
        }
