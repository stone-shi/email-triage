import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gmail_client import GmailClient


def make_client(service):
    client = GmailClient.__new__(GmailClient)
    client.settings = MagicMock(gmail_account="me@example.com")
    client.service = service
    return client


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
