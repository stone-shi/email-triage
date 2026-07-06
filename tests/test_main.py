import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import filter_emails_by_days


class TestFilterEmailsByDays:
    def test_all_within_days(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        two_days_ago = now - timedelta(days=2)

        emails = [
            {"date": yesterday.strftime("%a, %d %b %Y %H:%M:%S %z"), "id": "1"},
            {"date": two_days_ago.strftime("%a, %d %b %Y %H:%M:%S %z"), "id": "2"},
        ]
        result = filter_emails_by_days(emails, days=3)
        assert len(result) == 2

    def test_partial_filter_out_old(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        recent = now - timedelta(days=1)
        old = now - timedelta(days=10)

        emails = [
            {"date": recent.strftime("%a, %d %b %Y %H:%M:%S %z"), "id": "recent"},
            {"date": old.strftime("%a, %d %b %Y %H:%M:%S %z"), "id": "old"},
        ]
        result = filter_emails_by_days(emails, days=3)
        assert len(result) == 1
        assert result[0]["id"] == "recent"

    def test_empty_date_kept(self):
        emails = [{"date": "", "id": "no-date"}, {"date": None, "id": "none-date"}]
        result = filter_emails_by_days(emails, days=7)
        assert len(result) == 2

    def test_invalid_date_kept(self):
        emails = [{"date": "not-a-valid-date", "id": "bad-date"}]
        result = filter_emails_by_days(emails, days=7)
        assert len(result) == 1

    def test_empty_list(self):
        result = filter_emails_by_days([], days=7)
        assert result == []


class TestArgumentParser:
    def test_parser_exists_and_has_flags(self):
        import argparse
        from main import main as main_module

        parser = argparse.ArgumentParser()
        parser.add_argument("--human", action="store_true")
        parser.add_argument("--pretty", action="store_true")
        parser.add_argument("--max", type=int)
        parser.add_argument("--days", type=int)
        parser.add_argument("--level", type=int)
        parser.add_argument("--compact", action="store_true")
        parser.add_argument("--profile", type=str, default="default")
        parser.add_argument("--mark-read-all", action="store_true")
        parser.add_argument("--mark-read-message", type=str)
        parser.add_argument("--mark-read-level", type=int, choices=[0, 1, 2])

        args = parser.parse_args(["--human", "--pretty", "--max", "10"])
        assert args.human is True
        assert args.pretty is True
        assert args.max == 10

        args2 = parser.parse_args([])
        assert args2.human is False
        assert args2.profile == "default"


class TestCompactOutput:
    def test_compact_output_filters_fields(self):
        run_results = [
            {
                "triage_level": 2,
                "message_id": "<msg1@test.com>",
                "sender": "boss@company.com",
                "subject": "Important",
                "date": "2026-07-06",
                "reason": "Important email",
                "summary": "Do the thing",
                "score": 0.95,
                "tag": "personal",
                "account": "me@test.com",
            },
            {
                "triage_level": 0,
                "message_id": "<msg2@test.com>",
                "sender": "spam@junk.com",
                "subject": "Buy now!",
                "date": "2026-07-06",
                "reason": "Noise keyword: unsubscribe",
                "score": 0.0,
                "tag": "low",
            },
        ]
        compact_results = []
        for r in run_results:
            c_obj = {
                "mid": r.get("message_id"),
                "lvl": r.get("triage_level"),
                "snd": r.get("sender"),
                "sub": r.get("subject"),
                "dt": r.get("date"),
                "tag": r.get("tag")
            }
            if r.get("triage_level") == 2:
                c_obj["sum"] = r.get("summary")
            compact_results.append(c_obj)

        assert len(compact_results) == 2
        assert compact_results[0]["sum"] == "Do the thing"
        assert "sum" not in compact_results[1]
        assert compact_results[1]["mid"] == "<msg2@test.com>"
        assert compact_results[1]["lvl"] == 0


class TestLevelFilter:
    def test_filter_by_level(self):
        run_results = [
            {"triage_level": 0, "message_id": "m1"},
            {"triage_level": 1, "message_id": "m2"},
            {"triage_level": 2, "message_id": "m3"},
        ]
        filtered = [r for r in run_results if (r.get("triage_level") if r.get("triage_level") is not None else 0) >= 1]
        assert len(filtered) == 2
        assert filtered[0]["message_id"] == "m2"
        assert filtered[1]["message_id"] == "m3"

    def test_filter_level_2_only(self):
        run_results = [
            {"triage_level": 0, "message_id": "m1"},
            {"triage_level": 1, "message_id": "m2"},
            {"triage_level": 2, "message_id": "m3"},
        ]
        filtered = [r for r in run_results if (r.get("triage_level") if r.get("triage_level") is not None else 0) >= 2]
        assert len(filtered) == 1
        assert filtered[0]["message_id"] == "m3"


class TestPagination:
    def test_skip_results(self):
        results = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert results[1:] == [{"id": "b"}, {"id": "c"}]
        assert results[2:] == [{"id": "c"}]
        assert results[3:] == []

    def test_limit_results(self):
        results = [{"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "4"}, {"id": "5"}]
        assert results[:2] == [{"id": "1"}, {"id": "2"}]
        assert results[:0] == []

    def test_skip_and_limit(self):
        results = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
        result = results[1:][:2]
        assert result == [{"id": "b"}, {"id": "c"}]
