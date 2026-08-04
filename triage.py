import logging
import math
import re
import json
import time
from typing import Optional, Tuple, Dict, Any, List
from pydantic import BaseModel, Field, field_validator
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

# Transient-failure retry policy for every /chat/completions call below. Three upstream failures
# are worth retrying because the identical request usually succeeds moments later: a retryable
# 5xx (a self-hosted model backend that has died answers instantly with `rpc error: code =
# Unavailable` 500s until it is restarted, and a proxy reports a stalled generation as a 502), a
# dropped connection, and a 200 whose completion carries no content. Without a retry a single
# blip is silently absorbed as a *decision*: run_level_1_classification defaults the message to
# Level 2, and auto_rater_runner drops it from the benchmark population altogether. Outages
# measured on this deployment last ~10-20s, so the backoff is sized to outlast one rather than
# to be polite. Retries are bounded and never applied to a 4xx, which would just fail again.
LLM_RETRY_ATTEMPTS = 4
LLM_RETRY_BACKOFF_SECONDS = (1.0, 3.0, 8.0, 20.0)
_RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})


class _TransientLLMError(RuntimeError):
    """An upstream failure worth retrying: a retryable 5xx, or a completion with no content."""

    def __init__(self, message: str, response: Optional[httpx.Response] = None) -> None:
        super().__init__(message)
        self.response = response


# Zero-width and bidi-control characters carry no lexical meaning but are routinely used as
# filler in marketing mail -- most often long runs of U+200C to stretch a mail client's preview
# text. Stripping them before an LLM (or the reranker) ever sees the text is pure win on three
# counts: cost, since on this repo's 100-email benchmark set 20 messages carried them and on those
# they were 79-85% of the whole Level 1 prompt (9,067 -> 5,924 prompt tokens across the set);
# stability, since a run of them is what tipped localai/qwen3.6-35b-a3b into reasoning that never
# converged, returning the empty completions a proxy reports as a 502; and quality, since nothing
# visible to a human reader is removed, so the model judges what the recipient would actually see.
_INVISIBLE_CHARS_RE = re.compile(
    "["
    "­"          # soft hyphen
    "͏"          # combining grapheme joiner
    "؜"          # arabic letter mark
    "᠎"          # mongolian vowel separator
    "​‌"    # zero-width space, zero-width non-joiner
    "‎‏"    # left-to-right / right-to-left mark
    "‪-‮"   # bidi embedding and override controls
    "⁠-⁤"   # word joiner, invisible separator/times/plus
    "⁦-⁩"   # bidi isolates
    "﻿"          # zero-width no-break space (BOM)
    "]"
)

# U+200D (zero-width joiner) needs a narrower rule than the set above: it is filler between plain
# text -- all 308 occurrences in the benchmark set are single joiners wedged between ordinary
# characters or other zero-width filler -- but it is load-bearing *between* pictographs, where it
# builds a single glyph (👨‍👩‍👧). Drop it only when at least one neighbour is not a pictograph.
_EMOJI_ADJACENT = "\U0001f000-\U0001faff☀-➿⬀-⯿️⃣"
_FILLER_ZWJ_RE = re.compile(
    f"(?<![{_EMOJI_ADJACENT}])‍|‍(?![{_EMOJI_ADJACENT}])"
)


def strip_invisible(text: str) -> str:
    """Remove zero-width/bidi filler from text bound for an LLM or the reranker.

    Callers apply this *before* truncating, so a `full_body[:8000]` slice spends its budget on
    real content instead of padding. Visible text is left exactly as it was.
    """
    if not text:
        return text
    return _FILLER_ZWJ_RE.sub("", _INVISIBLE_CHARS_RE.sub("", text))


# A fenced block anywhere in the response, capturing its info string (```json / ``` / ```JSON)
# so a json-tagged fence can be preferred over an untagged one.
_FENCED_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_+-]*)[ \t]*\r?\n?(.*?)```", re.DOTALL)


def _first_balanced_object(text: str) -> Optional[str]:
    """The first balanced `{...}` span in text, or None. String-aware, so a brace inside a JSON
    string value (or an escaped quote) doesn't throw the depth count off."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _repair_json(text: str) -> str:
    """Fix the malformations models reliably produce inside an otherwise well-formed object."""
    # Some smaller models (like Qwen 0.8B) return unquoted tags: "tag": promotion
    # Look for "tag": followed by a single word that is NOT quoted and NOT a boolean/null.
    text = re.sub(r'("tag":\s*)(?!(?:true|false|null)\b)([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*[,}])', r'\1"\2"', text)
    # Invalid escapes like \' which some models return
    return text.replace("\\'", "'")


def extract_json(text: str) -> str:
    """Pull the JSON object out of an LLM completion that was asked for JSON only.

    Instruction-following is the exception, not the rule, so this is deliberately permissive:
    a model may wrap its object in a fence, bury the fence *after* several paragraphs of prose,
    or answer in prose with a bare object at the end. gemini-3.1-flash-lite does the second
    consistently -- a "### Executive Summary" preamble, then a ```json fence -- which the old
    `text.startswith("```")` test missed, so every Level 2 call it made failed to parse and the
    email was dropped from the benchmark population entirely.

    Candidates are tried in descending order of trustworthiness (json-tagged fence, untagged
    fence, the whole response, the first balanced `{...}` span of each of those) and the first
    one that actually parses after repair is returned. Ties within a tier go to document order,
    matching the older "keep only the first balanced span" behavior. If nothing parses, the
    best candidate is returned anyway so the caller's error log shows the most JSON-like text
    rather than a wall of prose.
    """
    text = (text or "").strip()

    fenced = _FENCED_BLOCK_RE.findall(text)
    candidates = [body.strip() for tag, body in fenced if tag.lower() == "json"]
    candidates += [body.strip() for tag, body in fenced if tag.lower() != "json"]
    candidates.append(text)
    # A candidate may still carry prose around its object (an unfenced answer, or a fence the
    # model narrated inside), so scan each for a balanced span as a second-chance candidate.
    for candidate in list(candidates):
        span = _first_balanced_object(candidate)
        if span and span != candidate:
            candidates.append(span)

    repaired = [_repair_json(c) for c in candidates if c]
    for candidate in repaired:
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            continue
    return repaired[0] if repaired else text


def _sigmoid(x: float) -> float:
    """Numerically-stable sigmoid, used to map a raw cross-encoder logit into (0,1)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)

def _join_bullet_list(value: Any) -> Any:
    """Accept a JSON array of bullets where the schema wants a single string.

    A model asked for a "bulleted" summary quite reasonably answers with an array, and rejecting
    it discarded an otherwise perfect result: the whole Level 2 stage failed and the caller stored
    "Failed to generate proxy summary due to error: 1 validation error" in place of the summary.
    Observed on localai/qwen3.6-35b-a3b (9 of 100 messages) once reasoning was disabled -- every
    field correct except `summary` arriving as ["...", "...", "..."]. Same spirit as the unquoted
    tag and bad-escape repairs in _extract_json: normalize what a model plausibly returns rather
    than lose the call to it. Anything that is not a list passes through untouched.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v) and str(v).strip()]
        return "\n".join(p if p.startswith(("-", "*", "•")) else f"- {p}" for p in parts)
    return value


class TriageDecision(BaseModel):
    suggested_level: int = Field(description="Suggested triage level: 0 (noise), 1 (notification/promotion), 2 (important)")
    reason: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")
    tag: str = Field(default="notification", description="One word classification tag (e.g., promotion, notification, personal, vip)")

    @field_validator("reason", mode="before")
    @classmethod
    def _accept_bullet_list(cls, v: Any) -> Any:
        return _join_bullet_list(v)

class SummaryResult(BaseModel):
    summary: str
    confidence_score: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")
    tag: str = Field(default="vip", description="One word classification tag (e.g., promotion, notification, personal, vip)")

    @field_validator("summary", mode="before")
    @classmethod
    def _accept_bullet_list(cls, v: Any) -> Any:
        return _join_bullet_list(v)

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

        # Per-instance overrides for the module-level retry policy, so a caller that wants a
        # different tolerance (or a test that wants no sleeping) can set them without patching
        # module state. See _post_chat_completion.
        self.llm_retry_attempts = LLM_RETRY_ATTEMPTS
        self.llm_retry_backoff_seconds = LLM_RETRY_BACKOFF_SECONDS

        # Extra key/values merged into every /chat/completions payload this engine sends. Empty
        # by default -- nothing in the normal CLI/MCP path sets it, so production behavior is
        # unchanged. It exists so a caller sweeping many models (auto_rater_runner) can pass
        # model-specific knobs such as `reasoning_effort`, which some local reasoning models
        # need in order to answer at all: without it they can spend the entire completion
        # budget on thinking tokens and return empty content. Set after construction, like the
        # `*_headers` dicts above.
        self.extra_payload_params: Dict[str, Any] = {}

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

    def _post_chat_completion(
        self, url: str, headers: Dict[str, str], payload: Dict[str, Any], stage: str,
    ) -> Tuple[httpx.Response, Dict[str, Any]]:
        """POST one /chat/completions request, retrying transient upstream failures.

        Returns (response, parsed_json), guaranteeing a non-empty `choices[0].message.content`.
        Once the attempts are exhausted it raises the last failure, which each caller's own
        `except` already turns into that stage's documented safe fallback -- so a total outage
        behaves exactly as it did before this retry existed, just later. Token spend on a failed
        attempt is deliberately not logged (an empty completion is the thing being retried past),
        so `token_logs` still gets one row per successful stage call.
        """
        attempts = max(1, int(self.llm_retry_attempts))
        backoff = self.llm_retry_backoff_seconds or (0.0,)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.http_client.post(url, headers=headers, json=payload)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    raise _TransientLLMError(
                        f"HTTP {response.status_code} from upstream: {response.text[:300]}", response
                    )
                response.raise_for_status()

                try:
                    resp_json = response.json()
                except json.JSONDecodeError:
                    # Not retried: a non-JSON 200 means a misconfigured request rather than a
                    # blip -- most often the proxy streaming Server-Sent Events because
                    # `stream: false` didn't reach it, which would fail identically forever.
                    logger.error(
                        "%s proxy response is not valid JSON. Status: %s, Content-Type: %s, Body: %s",
                        stage, response.status_code, response.headers.get("content-type"), response.text[:2000],
                    )
                    raise

                content = ((resp_json.get("choices") or [{}])[0].get("message") or {}).get("content")
                if not (content or "").strip():
                    raise _TransientLLMError(
                        f"upstream returned a completion with no content: {resp_json}", response
                    )
                return response, resp_json

            except (_TransientLLMError, httpx.TransportError) as err:
                last_error = err
                if attempt >= attempts:
                    break
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs", stage, attempt, attempts, err, delay
                )
                time.sleep(delay)

        logger.error("%s exhausted %d attempt(s) against transient upstream failures.", stage, attempts)
        raise last_error

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

        query_text = strip_invisible(f"From: {sender} | Subject: {subject} | Snippet: {snippet}")
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
        """Deprecated alias for the module-level `extract_json` (kept for existing callers,
        e.g. quality_check.py, which reach for it through a judge engine instance)."""
        return extract_json(text)

    def run_level_1_classification(self, sender: str, subject: str, snippet: str, model_name: Optional[str] = None) -> Tuple[int, str, float, str, Dict[str, Any]]:
        """
        Level 1 Triage: LiteLLM / DeepSeek flash ternary classification with JSON validation.
        Returns (suggested_level, reason, score, tag, metrics).
        """
        if not model_name:
            model_name = self.settings.triage_model

        sender, subject, snippet = (strip_invisible(sender), strip_invisible(subject), strip_invisible(snippet))
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
            **self.extra_payload_params,
        }

        try:
            logger.info("Level 1 Triage request sent to custom LiteLLM proxy model: %s", model_name)
            response, resp_json = self._post_chat_completion(
                url, self.triage_headers, payload, "Level 1 classification"
            )

            # Extract usage data from response if provided by proxy
            usage = resp_json.get("usage", {})
            metrics["prompt_tokens"] = usage.get("prompt_tokens", 0)
            metrics["completion_tokens"] = usage.get("completion_tokens", 0)
            
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 40)
            self.db.log_token_usage("level_1_classification", model_name, tokens_used)
            
            # Parse inner completion content (guaranteed non-empty by _post_chat_completion)
            content = resp_json["choices"][0]["message"]["content"]
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

        # Cleaned before the slice so the 8000-char budget goes to real content, not padding
        subject, full_body = strip_invisible(subject), strip_invisible(full_body)
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
            **self.extra_payload_params,
        }

        start_time = time.time()
        try:
            logger.info("Level 2 Triage summary request sent to custom LiteLLM proxy model: %s", model_name)
            response, resp_json = self._post_chat_completion(
                url, self.summary_headers, payload, "Level 2 summarization"
            )

            usage = resp_json.get("usage", {})
            metrics["prompt_tokens"] = usage.get("prompt_tokens", 0)
            metrics["completion_tokens"] = usage.get("completion_tokens", 0)
            
            tokens_used = usage.get("total_tokens", self._estimate_tokens(prompt) + 180)
            self.db.log_token_usage("level_2_summary", model_name, tokens_used)
            
            # Guaranteed non-empty by _post_chat_completion
            content = resp_json["choices"][0]["message"]["content"].strip()
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
        # Cleaned before the slice so the 6000-char budget goes to real content, not padding
        sender, subject, snippet, full_body = (strip_invisible(sender), strip_invisible(subject),
                                               strip_invisible(snippet), strip_invisible(full_body))
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
            **self.extra_payload_params,
        }

        try:
            logger.info("Ambiguity Triage Escalation sent to premium model: %s", self.settings.summary_model)
            response, resp_json = self._post_chat_completion(
                url, self.summary_headers, payload, "Premium triage escalation"
            )

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
