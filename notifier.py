import logging
import json
from typing import Dict, Any

logger = logging.getLogger("email_triage.notifier")

class EmailNotifier:
    @staticmethod
    def print_level_0_hit(message_id: str, account: str, subject: str, reason: str) -> None:
        """Outputs structured details for Level 0 noise filter hits."""
        border = "-" * 50
        output = f"""
[TRIAGE LEVEL]: Level 0 (Static Noise Filter Hit)
🆔 MESSAGE ID: {message_id}
📧 ACCOUNT:    {account}
📝 SUBJECT:    {subject}
💡 REASON:     {reason}
{border}"""
        print(output)

    @staticmethod
    def print_level_1_hit(message_id: str, account: str, subject: str, reason: str, score: float) -> None:
        """Outputs structured details for Level 1 unimportant classification outcomes."""
        border = "-" * 50
        output = f"""
[TRIAGE LEVEL]: Level 1 (LLM Unimportant Filter Hit)
🆔 MESSAGE ID: {message_id}
📧 ACCOUNT:    {account}
📝 SUBJECT:    {subject}
💡 REASON:     {reason}
🔢 SCORE:      {score:.2f}
{border}"""
        print(output)

    @staticmethod
    def print_terminal_banner(subject: str, sender: str, reason: str, summary: str, score: float) -> None:
        """Prints structured human-readable terminal card for Level 2 alerts."""
        border = "=" * 80
        card = f"""
[TRIAGE LEVEL]: Level 2 (High Importance Premium Escalation)
{border}
📬 FROM:    {sender}
📧 SUBJECT: {subject}
💡 REASON:  {reason}
🔢 SCORE:   {score:.2f}
--------------------------------------------------------------------------------
📝 SUMMARY:
{summary}
{border}
"""
        print(card)

    @staticmethod
    def print_json_payload(email_data: Dict[str, Any]) -> None:
        """Outputs parseable JSON payload string block demarcated by tags."""
        payload = {
            "triage_level": "Level 2",
            "message_id": email_data.get("message_id"),
            "account": email_data.get("account"),
            "sender": email_data.get("sender"),
            "subject": email_data.get("subject"),
            "date": email_data.get("date"),
            "triage_reason": email_data.get("reason"),
            "summary": email_data.get("summary"),
            "score": email_data.get("score")
        }
        json_string = json.dumps(payload, indent=2, ensure_ascii=False)
        print(f"\n[JSON_OUTPUT_START]\n{json_string}\n[JSON_OUTPUT_END]\n" + "-" * 40 + "\n")
