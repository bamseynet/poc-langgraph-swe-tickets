from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ticketgate.jira import JiraSource, adf_to_text

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jira"
BASE = "https://example.atlassian.net"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _source(handler: Any, jql: str = "project = SHOP", **kwargs: Any) -> JiraSource:
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(handler))
    return JiraSource(client, base_url=BASE, jql=jql, **kwargs)


def _two_pages(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("nextPageToken") == "page-2":
        return httpx.Response(200, json=_fixture("search_page2.json"))
    return httpx.Response(200, json=_fixture("search_page1.json"))


def test_maps_a_jira_issue_onto_a_ticket() -> None:
    ticket = _source(_two_pages).list_tickets()[0]
    assert ticket.ref.source == "jira"
    assert ticket.ref.project == "SHOP"
    assert ticket.ref.key == "SHOP-7"
    assert ticket.ref.thread_id == "jira:SHOP#SHOP-7"
    assert ticket.title == "Checkout crashes on 100% discount"
    assert ticket.author == "Ada Reporter"
    assert ticket.url == f"{BASE}/browse/SHOP-7"
    assert ticket.created_at == datetime(2026, 3, 1, 10, 15, 30, tzinfo=UTC)


def test_keeps_jira_type_and_priority_as_labels_for_triage() -> None:
    ticket = _source(_two_pages).list_tickets()[0]
    assert ticket.labels == ["checkout", "jira-type:Bug", "jira-priority:High"]


def test_flattens_the_atlassian_document_format_body() -> None:
    body = _source(_two_pages).list_tickets()[0].body
    assert "Problem" in body
    assert "Total is wrong\nafter the coupon." in body
    assert "- Apply FREE100" in body
    assert "total / 0" in body


def test_maps_null_optional_fields() -> None:
    ticket = _source(_two_pages).list_tickets()[1]
    assert ticket.body == ""
    assert ticket.author == "unknown"
    assert ticket.labels == ["jira-type:Task"]


def test_follows_next_page_tokens() -> None:
    assert [t.ref.key for t in _source(_two_pages).list_tickets()] == ["SHOP-7", "SHOP-8"]


def test_uses_the_enhanced_search_endpoint_with_the_configured_jql() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=_fixture("search_page2.json"))

    _source(handler, jql="project = SHOP AND status = Open").list_tickets()
    assert seen[0].path == "/rest/api/3/search/jql"
    assert seen[0].params["jql"] == "project = SHOP AND status = Open"
    assert "summary" in seen[0].params["fields"]


def test_a_repeating_page_token_cannot_loop_forever() -> None:
    # Reported against the live API: a token that never ends.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("search_page1.json"))

    assert len(_source(handler).list_tickets()) == 1


def test_stops_at_max_tickets() -> None:
    assert len(_source(_two_pages, max_tickets=1).list_tickets()) == 1


def test_raises_on_an_api_error() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _source(lambda _: httpx.Response(401, json={})).list_tickets()


def test_adf_tolerates_unknown_nodes_and_plain_text() -> None:
    assert adf_to_text(None) == ""
    assert adf_to_text("legacy plain text") == "legacy plain text"
    unknown_node = {"type": "mystery", "content": [{"type": "text", "text": "x"}]}
    doc = {"type": "doc", "content": [unknown_node]}
    assert adf_to_text(doc) == "x"
