import sys
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from notifier import EmailNotifier


class TestPrintLevel0Hit:
    def test_output_contains_message_id(self, capsys):
        EmailNotifier.print_level_0_hit(
            "<abc123@mail.com>", "user@test.com", "Spam Subject", "Noise keyword: spam"
        )
        captured = capsys.readouterr()
        assert "Level 0" in captured.out
        assert "<abc123@mail.com>" in captured.out
        assert "user@test.com" in captured.out
        assert "Spam Subject" in captured.out
        assert "Noise keyword: spam" in captured.out

    def test_output_has_border(self, capsys):
        EmailNotifier.print_level_0_hit("id", "account", "subject", "reason")
        captured = capsys.readouterr()
        assert "-" * 50 in captured.out


class TestPrintLevel1Hit:
    def test_output_contains_score(self, capsys):
        EmailNotifier.print_level_1_hit(
            "<def456@mail.com>", "user@zoho.com", "Promo", "Not important", 0.65
        )
        captured = capsys.readouterr()
        assert "Level 1" in captured.out
        assert "<def456@mail.com>" in captured.out
        assert "0.65" in captured.out

    def test_output_has_border(self, capsys):
        EmailNotifier.print_level_1_hit("id", "account", "subj", "reason", 0.5)
        captured = capsys.readouterr()
        assert captured.out.count("-") > 10


class TestPrintTerminalBanner:
    def test_output_contains_all_fields(self, capsys):
        EmailNotifier.print_terminal_banner(
            "Urgent Meeting",
            "boss@company.com",
            "High priority personal email",
            "- Review slides\n- Prepare Q&A",
            0.97
        )
        captured = capsys.readouterr()
        assert "Level 2" in captured.out
        assert "boss@company.com" in captured.out
        assert "Urgent Meeting" in captured.out
        assert "High priority personal email" in captured.out
        assert "Review slides" in captured.out
        assert "0.97" in captured.out

    def test_output_has_full_width_border(self, capsys):
        EmailNotifier.print_terminal_banner("S", "s@t.com", "r", "summary", 1.0)
        captured = capsys.readouterr()
        assert "=" * 80 in captured.out


class TestPrintJSONPayload:
    def test_produces_valid_json(self, capsys):
        email_data = {
            "message_id": "<json@test.com>",
            "account": "user@test.com",
            "sender": "sender@test.com",
            "subject": "JSON Test",
            "date": "2026-01-01",
            "reason": "Test reason",
            "summary": "Test summary",
            "score": 0.85
        }
        EmailNotifier.print_json_payload(email_data)
        captured = capsys.readouterr()
        assert "JSON_OUTPUT_START" in captured.out
        assert "<json@test.com>" in captured.out
        assert "JSON_OUTPUT_END" in captured.out

    def test_output_is_parseable_json(self, capsys):
        import json
        email_data = {"message_id": "id", "account": "acc", "sender": "snd", "subject": "sub", "date": "dt", "reason": "r", "summary": "s", "score": 0.5}
        EmailNotifier.print_json_payload(email_data)
        captured = capsys.readouterr()
        start = captured.out.find("[JSON_OUTPUT_START]") + len("[JSON_OUTPUT_START]")
        end = captured.out.find("[JSON_OUTPUT_END]")
        json_str = captured.out[start:end].strip()
        parsed = json.loads(json_str)
        assert parsed["triage_level"] == 2
        assert parsed["message_id"] == "id"
