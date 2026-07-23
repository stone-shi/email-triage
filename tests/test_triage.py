import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from triage import EmailTriageEngine, TriageDecision, SummaryResult
from db import EmailDB


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.workspace_dir = Path(__file__).parent.parent
    s.llm_base_url = "https://proxy.example.com/v1"
    s.llm_api_key = "test-key"
    s.triage_base_url = "https://triage-proxy.example.com/v1"
    s.triage_api_key = "triage-key"
    s.summary_base_url = "https://summary-proxy.example.com/v1"
    s.summary_api_key = "summary-key"
    s.triage_model = "deepseek/triage-flash"
    s.summary_model = "deepseek/summary-pro"

    triage_config = MagicMock()
    triage_config.confidence_threshold = 0.8
    triage_config.tei_router_enabled = False
    triage_config.tei_url = "https://rerank.example.com/v1/rerank"
    triage_config.tei_model = "localai/qwen3-reranker-0.6b"
    triage_config.tei_api_key = "tei-key"
    triage_config.tei_noise_enabled = True
    triage_config.tei_signal_enabled = True
    triage_config.tei_noise_threshold = 0.999
    triage_config.tei_signal_threshold = 0.95
    triage_config.whitelist_vip_senders = []
    triage_config.whitelist_domains = []
    triage_config.blacklist_keywords = [
        "unsubscribe", "newsletter", "promotions", "marketing",
        "no-reply", "noreply", "digest", "advertisement"
    ]
    triage_config.blacklist_senders = ["spammer@domain.com", "offers@", "newsletters@"]
    triage_config.triage_type = "llm"
    s.triage = triage_config
    return s


@pytest.fixture
def mock_db():
    db = MagicMock(spec=EmailDB)
    return db


@pytest.fixture
def engine(mock_db, mock_settings):
    return EmailTriageEngine(db=mock_db, settings_instance=mock_settings)


class TestEmailTriageEngineInit:
    def test_base_urls_stripped(self, mock_db, mock_settings):
        mock_settings.llm_base_url = "https://proxy.example.com/v1/"
        engine = EmailTriageEngine(db=mock_db, settings_instance=mock_settings)
        assert engine.base_url == "https://proxy.example.com/v1"

    def test_headers_contain_api_key(self, engine):
        assert "Authorization" in engine.headers
        assert "Bearer" in engine.headers["Authorization"]

    def test_separate_triage_summary_headers(self, engine):
        assert engine.triage_headers["Authorization"] == "Bearer triage-key"
        assert engine.summary_headers["Authorization"] == "Bearer summary-key"


class TestLevel0StaticFilter:
    def test_noise_subject_unsubscribe(self, engine):
        is_noise, reason = engine.run_level_0_static("sender@test.com", "Please unsubscribe me")
        assert is_noise is True
        assert "unsubscribe" in reason.lower()

    def test_noise_subject_newsletter(self, engine):
        is_noise, reason = engine.run_level_0_static("noreply@news.com", "Weekly Newsletter Digest")
        assert is_noise is True

    def test_noise_sender_no_reply(self, engine):
        is_noise, reason = engine.run_level_0_static("no-reply@company.com", "Your order update")
        assert is_noise is True

    def test_noise_blacklist_sender(self, engine):
        is_noise, reason = engine.run_level_0_static("offers@deals.com", "Great deals today!")
        assert is_noise is True
        assert "sender pattern" in reason.lower()

    def test_not_noise_personal_email(self, engine):
        is_noise, reason = engine.run_level_0_static("friend@personal.com", "Dinner tonight?")
        assert is_noise is False
        assert reason is None

    def test_not_noise_work_email(self, engine):
        is_noise, reason = engine.run_level_0_static("boss@company.com", "Q3 Planning Review")
        assert is_noise is False

    def test_whitelist_domain_bypasses_filter(self, engine):
        engine.settings.triage.whitelist_domains = ["important-partner.com"]
        is_noise, reason = engine.run_level_0_static(
            "support@important-partner.com",
            "Unsubscribe from our great newsletter"
        )
        assert is_noise is False

    def test_blacklist_keywords_case_insensitive(self, engine):
        is_noise, reason = engine.run_level_0_static(
            "someone@test.com",
            "UNSUBSCRIBE from marketing emails"
        )
        assert is_noise is True


class TestVIPWhitelist:
    def test_is_vip_sender_match(self, engine):
        engine.settings.triage.whitelist_vip_senders = ["boss@company.com", "ceo@company.com"]
        assert engine.is_vip_sender("boss@company.com") is True
        assert engine.is_vip_sender("ceo@company.com") is True

    def test_is_vip_sender_no_match(self, engine):
        engine.settings.triage.whitelist_vip_senders = ["boss@company.com"]
        assert engine.is_vip_sender("stranger@gmail.com") is False

    def test_is_vip_partial_match(self, engine):
        engine.settings.triage.whitelist_vip_senders = ["family"]
        assert engine.is_vip_sender("mom@family.com") is True

    def test_is_vip_case_insensitive(self, engine):
        engine.settings.triage.whitelist_vip_senders = ["BOSS@COMPANY.COM"]
        assert engine.is_vip_sender("boss@company.com") is True


class TestExtractJSON:
    def test_extract_plain_json(self, engine):
        result = engine._extract_json('{"key": "value"}')
        assert result == '{"key": "value"}'

    def test_extract_json_with_markdown_block(self, engine):
        result = engine._extract_json('```json\n{"key": "value"}\n```')
        assert result == '{"key": "value"}'

    def test_extract_json_with_plain_markdown_block(self, engine):
        result = engine._extract_json('```\n{"key": "value"}\n```')
        assert result == '{"key": "value"}'

    def test_extract_fix_unquoted_tag(self, engine):
        result = engine._extract_json('{"tag": promotion, "score": 0.9}')
        parsed = json.loads(result)
        assert parsed["tag"] == "promotion"

    def test_extract_preserves_quoted_tag(self, engine):
        result = engine._extract_json('{"tag": "vip", "score": 0.95}')
        parsed = json.loads(result)
        assert parsed["tag"] == "vip"

    def test_extract_tag_not_boolean(self, engine):
        result = engine._extract_json('{"tag": true, "score": 0.9}')
        parsed = json.loads(result)
        assert parsed["tag"] is True

    def test_extract_fix_escaped_quotes(self, engine):
        result = engine._extract_json("""{"summary": "He said: \\'hello\\'"}""")
        parsed = json.loads(result)
        assert "hello" in parsed["summary"]


class TestTokenEstimation:
    def test_estimate_tokens_with_encoder(self, engine):
        tokens = engine._estimate_tokens("Hello world this is a test")
        assert isinstance(tokens, int)
        assert tokens > 0

    def test_estimate_tokens_fallback(self, engine):
        engine.encoder = None
        text = "Hello world this is a test message with some length"
        tokens = engine._estimate_tokens(text)
        assert tokens >= len(text) // 4


class TestRunLevel1Classification:
    def test_successful_classification_important(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 2, "reason": "Action required", "confidence_score": 0.95, "tag": "personal"}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "boss@company.com", "Urgent: Q3 Deadline", "Need the report by Friday"
            )
        assert level == 2
        assert reason == "Action required"
        assert score == 0.95
        assert tag == "personal"
        assert metrics["prompt_tokens"] == 50
        assert metrics["completion_tokens"] == 20

    def test_successful_classification_unimportant(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 1, "reason": "Promotional offer", "confidence_score": 0.75, "tag": "promotion"}'}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "offers@store.com", "50% off today!", "Big sale happening now"
            )
        assert level == 1
        assert tag == "promotion"

    def test_successful_classification_noise(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 0, "reason": "Pure spam", "confidence_score": 0.99, "tag": "low"}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "spam@junk.com", "WINNER!!!", "You won a prize"
            )
        assert level == 0
        assert tag == "low"

    def test_request_failure_falls_back_to_level_2(self, engine):
        with patch.object(engine.http_client, "post", side_effect=httpx.HTTPError("Connection refused")):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "someone@test.com", "Test", "Test body"
            )
        assert level == 2
        assert "error" in reason.lower()

    def test_empty_llm_response_raises(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}],
            "usage": {}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "test@test.com", "Test", "Test"
            )
        assert level == 2

    def test_json_in_markdown_code_block(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '```json\n{"suggested_level": 1, "reason": "Coupon offer", "confidence_score": 0.7, "tag": "promotion"}\n```'}}],
            "usage": {"total_tokens": 50}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "deals@shop.com", "Your coupon", "Save 20%"
            )
        assert level == 1

    def test_custom_model_name(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 0, "reason": "Noise", "confidence_score": 0.9, "tag": "low"}'}}],
            "usage": {"total_tokens": 40}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response) as mock_post:
            engine.run_level_1_classification("s@t.com", "S", "b", model_name="custom-model")
            call_args = mock_post.call_args
            payload = call_args[1]["json"]
            assert payload["model"] == "custom-model"


class TestRunLevel2Summarization:
    def test_successful_summarization(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "Action: Review Q3 report. Deadline: Friday.", "confidence_score": 0.93, "tag": "personal"}'}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            summary, score, tag, metrics = engine.run_level_2_summarization(
                "Q3 Report", "Please review the attached Q3 report by Friday. Let me know your thoughts."
            )
        assert "Review Q3 report" in summary
        assert score == 0.93
        assert tag == "personal"
        assert metrics["prompt_tokens"] == 200

    def test_empty_body_returns_placeholder(self, engine):
        summary, score, tag, metrics = engine.run_level_2_summarization("Empty", "")
        assert "No substantive content" in summary
        assert score == 0.0
        assert tag == "notification"

    def test_short_body_returns_placeholder(self, engine):
        summary, score, tag, metrics = engine.run_level_2_summarization("Short", "Hi")
        assert "No substantive content" in summary

    def test_request_failure_returns_error_summary(self, engine):
        with patch.object(engine.http_client, "post", side_effect=httpx.HTTPError("Timeout")):
            summary, score, tag, metrics = engine.run_level_2_summarization(
                "Test", "This is a long enough body text to summarize properly."
            )
        assert "Failed to generate" in summary
        assert score == 1.0
        assert tag == "vip"

    def test_custom_model_name(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "Test.", "confidence_score": 0.8, "tag": "update"}'}}],
            "usage": {"total_tokens": 100}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response) as mock_post:
            engine.run_level_2_summarization("S", "Body text long enough to process", model_name="custom-pro")
            payload = mock_post.call_args[1]["json"]
            assert payload["model"] == "custom-pro"


class TestRunLevel1PremiumEscalation:
    def test_successful_escalation(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 1, "reason": "Low priority notification", "confidence_score": 0.85, "tag": "notification"}'}}],
            "usage": {"total_tokens": 120}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag = engine.run_level_1_premium_escalation(
                "system@service.com", "Weekly Digest", "Your weekly report", "Full body content here..."
            )
        assert level == 1
        assert score == 0.85

    def test_escalation_failure_returns_level_2(self, engine):
        with patch.object(engine.http_client, "post", side_effect=httpx.HTTPError("Server error")):
            level, reason, score, tag = engine.run_level_1_premium_escalation(
                "test@test.com", "Test", "Test snippet", "Test body"
            )
        assert level == 2
        assert "error" in reason.lower()


class TestChatCompletionsDisablesStreaming:
    """
    Regression coverage: the LLM proxy will return a Server-Sent-Events stream instead of a
    single JSON body for /chat/completions calls that don't explicitly disable it, which breaks
    response.json() parsing and silently defaults every classification to Level 2. Every payload
    must set stream=False.
    """

    def test_level_1_classification_disables_streaming(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 0, "reason": "r", "confidence_score": 0.9, "tag": "low"}'}}],
            "usage": {"total_tokens": 40}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response) as mock_post:
            engine.run_level_1_classification("s@t.com", "S", "b")
            assert mock_post.call_args[1]["json"]["stream"] is False

    def test_level_2_summarization_disables_streaming(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"summary": "s", "confidence_score": 0.9, "tag": "personal"}'}}],
            "usage": {"total_tokens": 40}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response) as mock_post:
            engine.run_level_2_summarization("Subject", "Body long enough to summarize properly.")
            assert mock_post.call_args[1]["json"]["stream"] is False

    def test_premium_escalation_disables_streaming(self, engine):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"suggested_level": 1, "reason": "r", "confidence_score": 0.85, "tag": "notification"}'}}],
            "usage": {"total_tokens": 40}
        }
        with patch.object(engine.http_client, "post", return_value=mock_response) as mock_post:
            engine.run_level_1_premium_escalation("s@t.com", "S", "snip", "body")
            assert mock_post.call_args[1]["json"]["stream"] is False


class TestTEIRouter:
    def test_tei_router_disabled(self, engine):
        engine.settings.triage.tei_router_enabled = False
        override_level, reason, confidence = engine.run_tei_router(
            "sender@test.com", "Subject", "Snippet"
        )
        assert override_level is None
        assert reason is None
        assert confidence == 1.0

    def test_tei_router_signal_escalation(self, engine):
        engine.settings.triage.tei_router_enabled = True
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.96},
                {"index": 1, "relevance_score": 0.01},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response) as mock_post:
            override_level, reason, confidence = engine.run_tei_router(
                "boss@company.com", "Urgent Q3 Report", "Please review the report"
            )
        assert override_level == 2
        assert "Rerank Signal" in reason
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"]["model"] == "localai/qwen3-reranker-0.6b"
        assert call_kwargs["headers"]["Authorization"] == "Bearer tei-key"

    def test_tei_router_noise_filter(self, engine):
        engine.settings.triage.tei_router_enabled = True
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.9995},
                {"index": 0, "relevance_score": 0.0001},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            override_level, reason, confidence = engine.run_tei_router(
                "spam@junk.com", "BUY NOW", "Limited time offer"
            )
        assert override_level == 0
        assert "Rerank Noise" in reason

    def test_tei_router_signal_direction_disabled(self, engine):
        engine.settings.triage.tei_router_enabled = True
        engine.settings.triage.tei_signal_enabled = False
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.96},
                {"index": 1, "relevance_score": 0.01},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            override_level, reason, confidence = engine.run_tei_router(
                "boss@company.com", "Urgent Q3 Report", "Please review the report"
            )
        assert override_level is None

    def test_tei_router_noise_direction_disabled(self, engine):
        engine.settings.triage.tei_router_enabled = True
        engine.settings.triage.tei_noise_enabled = False
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.9995},
                {"index": 0, "relevance_score": 0.0001},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            override_level, reason, confidence = engine.run_tei_router(
                "spam@junk.com", "BUY NOW", "Limited time offer"
            )
        assert override_level is None

    def test_tei_router_ambiguous(self, engine):
        engine.settings.triage.tei_router_enabled = True
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.4},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            override_level, reason, confidence = engine.run_tei_router(
                "news@example.com", "Daily Brief", "Today's news"
            )
        assert override_level is None

    def test_tei_router_request_failure(self, engine):
        engine.settings.triage.tei_router_enabled = True
        with patch.object(engine.http_client, "post", side_effect=httpx.HTTPError("Rerank down")):
            override_level, reason, confidence = engine.run_tei_router(
                "test@test.com", "Test", "Snippet"
            )
        assert override_level is None
        assert confidence == 0.0


class TestPydanticModels:
    def test_triage_decision_valid(self):
        td = TriageDecision(suggested_level=2, reason="Important", confidence_score=0.95, tag="personal")
        assert td.suggested_level == 2
        assert td.tag == "personal"

    def test_triage_decision_defaults(self):
        td = TriageDecision(suggested_level=1, reason="Notification")
        assert td.confidence_score == 1.0
        assert td.tag == "notification"

    def test_triage_decision_boundary_values(self):
        td = TriageDecision(suggested_level=0, reason="Zero")
        assert td.suggested_level == 0
        td = TriageDecision(suggested_level=2, reason="Two")
        assert td.suggested_level == 2

    def test_summary_result_valid(self):
        sr = SummaryResult(summary="Test summary", confidence_score=0.88, tag="vip")
        assert sr.summary == "Test summary"

    def test_summary_result_defaults(self):
        sr = SummaryResult(summary="Summary")
        assert sr.confidence_score == 1.0
        assert sr.tag == "vip"


class TestTEIClassifierPath:
    def test_tei_classification_important(self, engine):
        engine.settings.triage.triage_type = "tei"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.05},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "boss@company.com", "Important", "Action needed"
            )
        assert level == 2
        assert "Rerank Classifier" in reason

    def test_tei_classification_not_important(self, engine):
        engine.settings.triage.triage_type = "tei"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.85},
                {"index": 0, "relevance_score": 0.1},
            ]
        }
        with patch.object(engine.http_client, "post", return_value=mock_response):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "news@site.com", "Weekly newsletter", "Latest updates"
            )
        assert level == 1
        assert tag == "notification"

    def test_tei_classification_failure_falls_back(self, engine):
        engine.settings.triage.triage_type = "tei"
        with patch.object(engine.http_client, "post", side_effect=httpx.HTTPError("Rerank down")):
            level, reason, score, tag, metrics = engine.run_level_1_classification(
                "test@test.com", "Test", "Body"
            )
        assert level == 2
        assert "error" in reason.lower()
