import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import gmail_client
from gmail_client import GmailClient


def make_client(service):
    client = GmailClient.__new__(GmailClient)
    client.settings = MagicMock(gmail_account="me@example.com")
    client.service = service
    return client


class FakeHttpError(Exception):
    """Mimics googleapiclient.errors.HttpError's shape (.resp.status) without its constructor."""
    def __init__(self, status):
        self.resp = type("Resp", (), {"status": status})()


class FakeBatch:
    """Records .add() calls and replays a scripted callback outcome per request_id on .execute()."""
    def __init__(self, callback, outcomes_by_id):
        self.callback = callback
        self.outcomes_by_id = outcomes_by_id
        self.request_ids = []

    def add(self, request, request_id=None):
        self.request_ids.append(request_id)

    def execute(self):
        for rid in self.request_ids:
            outcomes = self.outcomes_by_id[rid]
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(outcome, Exception):
                self.callback(rid, None, outcome)
            else:
                self.callback(rid, outcome, None)


class TestFetchUnreadMessagesPagination:
    def test_single_page(self):
        service = MagicMock()
        list_mock = service.users.return_value.messages.return_value.list
        list_mock.return_value.execute.return_value = {"messages": [{"id": "1"}, {"id": "2"}]}
        client = make_client(service)
        client._fetch_metadata_batch = MagicMock(return_value=["meta1", "meta2"])

        result = client.fetch_unread_messages()

        assert result == ["meta1", "meta2"]
        client._fetch_metadata_batch.assert_called_once_with([{"id": "1"}, {"id": "2"}])

    def test_follows_next_page_token(self):
        service = MagicMock()
        list_mock = service.users.return_value.messages.return_value.list
        page1 = {"messages": [{"id": "1"}], "nextPageToken": "tok"}
        page2 = {"messages": [{"id": "2"}]}
        list_mock.return_value.execute.side_effect = [page1, page2]
        client = make_client(service)
        client._fetch_metadata_batch = MagicMock(side_effect=lambda msgs: msgs)

        result = client.fetch_unread_messages()

        assert result == [{"id": "1"}, {"id": "2"}]
        assert list_mock.call_count == 2
        _, second_kwargs = list_mock.call_args_list[1]
        assert second_kwargs.get("pageToken") == "tok"

    def test_stops_at_max_results_across_pages(self):
        service = MagicMock()
        list_mock = service.users.return_value.messages.return_value.list
        page1 = {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "tok"}
        page2 = {"messages": [{"id": "3"}, {"id": "4"}]}
        list_mock.return_value.execute.side_effect = [page1, page2]
        client = make_client(service)
        client._fetch_metadata_batch = MagicMock(side_effect=lambda msgs: msgs)

        result = client.fetch_unread_messages(max_results=3)

        assert len(result) == 3

    def test_no_messages_skips_metadata_fetch(self):
        service = MagicMock()
        list_mock = service.users.return_value.messages.return_value.list
        list_mock.return_value.execute.return_value = {"messages": []}
        client = make_client(service)
        client._fetch_metadata_batch = MagicMock()

        result = client.fetch_unread_messages()

        assert result == []
        client._fetch_metadata_batch.assert_not_called()


class TestFetchMetadataBatchRetry:
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {"m1": [FakeHttpError(429), {"id": "m1", "payload": {"headers": []}, "snippet": "hi"}]}
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client._fetch_metadata_batch([{"id": "m1"}])

        assert len(result) == 1
        assert result[0]["id"] == "m1"
        assert service.new_batch_http_request.call_count == 2

    def test_gives_up_after_max_retries_on_persistent_429(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {"m1": [FakeHttpError(429)]}
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client._fetch_metadata_batch([{"id": "m1"}], max_retries=2)

        assert result == []
        assert service.new_batch_http_request.call_count == 3  # initial attempt + 2 retries

    def test_non_retryable_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {"m1": [FakeHttpError(404)]}
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client._fetch_metadata_batch([{"id": "m1"}], max_retries=4)

        assert result == []
        assert service.new_batch_http_request.call_count == 1

    def test_mixed_success_and_retry_within_same_batch(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {
            "m1": [{"id": "m1", "payload": {"headers": []}, "snippet": "ok immediately"}],
            "m2": [FakeHttpError(429), {"id": "m2", "payload": {"headers": []}, "snippet": "ok after retry"}],
        }
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client._fetch_metadata_batch([{"id": "m1"}, {"id": "m2"}])

        assert {r["id"] for r in result} == {"m1", "m2"}


class TestFetchFullBodiesBatch:
    def test_fetches_multiple_bodies_in_one_batch(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {
            "m1": [{"payload": {}, "snippet": "body one"}],
            "m2": [{"payload": {}, "snippet": "body two"}],
        }
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client.fetch_full_bodies_batch(["m1", "m2"])

        assert result == {"m1": "body one", "m2": "body two"}
        assert service.new_batch_http_request.call_count == 1

    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {"m1": [FakeHttpError(429), {"payload": {}, "snippet": "recovered body"}]}
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client.fetch_full_bodies_batch(["m1"])

        assert result == {"m1": "recovered body"}
        assert service.new_batch_http_request.call_count == 2

    def test_gives_up_after_max_retries_omits_id(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        outcomes = {"m1": [FakeHttpError(429)]}
        service.new_batch_http_request.side_effect = lambda callback: FakeBatch(callback, outcomes)
        client = make_client(service)

        result = client.fetch_full_bodies_batch(["m1"], max_retries=2)

        assert result == {}
        assert service.new_batch_http_request.call_count == 3

    def test_empty_input_returns_empty_dict_without_calling_service(self):
        service = MagicMock()
        client = make_client(service)

        result = client.fetch_full_bodies_batch([])

        assert result == {}
        service.new_batch_http_request.assert_not_called()


class TestFetchFullBodyRetry:
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        execute_mock = service.users.return_value.messages.return_value.get.return_value.execute
        execute_mock.side_effect = [FakeHttpError(429), {"payload": {}, "snippet": "fallback text"}]
        client = make_client(service)

        body = client.fetch_full_body("m1")

        assert body == "fallback text"
        assert execute_mock.call_count == 2

    def test_gives_up_after_max_retries_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        execute_mock = service.users.return_value.messages.return_value.get.return_value.execute
        execute_mock.side_effect = FakeHttpError(429)
        client = make_client(service)

        body = client.fetch_full_body("m1", max_retries=2)

        assert body == ""
        assert execute_mock.call_count == 3  # initial attempt + 2 retries

    def test_non_retryable_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(gmail_client.time, "sleep", lambda s: None)
        service = MagicMock()
        execute_mock = service.users.return_value.messages.return_value.get.return_value.execute
        execute_mock.side_effect = FakeHttpError(404)
        client = make_client(service)

        body = client.fetch_full_body("m1", max_retries=4)

        assert body == ""
        assert execute_mock.call_count == 1
