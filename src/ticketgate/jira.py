"""Jira Cloud adapter: off by default and never connected to a Jira instance.

It shows that the `TicketSource` interface fits a second system. Enable it with
`JIRA_ENABLED=true`. There is no Jira writer yet, so approving a Jira ticket fails with a
clear error and nothing is written.

Uses `GET /rest/api/3/search/jql`, which replaced the removed `/rest/api/3/search`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ticketgate.models import Ticket, TicketRef

SOURCE_NAME = "jira"
PAGE_SIZE = 50
_FIELDS = "summary,description,issuetype,priority,labels,reporter,project,created"
_BLOCK_NODES = {"paragraph", "heading", "codeBlock", "blockquote", "rule"}


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format body to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    return _render_adf(node).strip()


def _render_adf(node: dict[str, Any]) -> str:
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    inner = "".join(_render_adf(child) for child in node.get("content", []))
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in _BLOCK_NODES:
        return f"{inner}\n\n"
    return inner


def _parse_jira_time(value: str) -> datetime:
    # Jira writes offsets without a colon: 2026-03-01T10:15:30.000+0000
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


class JiraSource:
    def __init__(self, client: httpx.Client, *, base_url: str, jql: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._jql = jql

    def list_tickets(self) -> list[Ticket]:
        tickets: dict[str, Ticket] = {}
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = self._fetch_page(token)
            for issue in page.get("issues", []):
                tickets.setdefault(issue["key"], self._to_ticket(issue))
            token = page.get("nextPageToken")
            # A repeated token means the API is looping, so stop rather than spin forever.
            if page.get("isLast", True) or not token or token in seen_tokens:
                return list(tickets.values())
            seen_tokens.add(token)

    def _fetch_page(self, token: str | None) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "jql": self._jql,
            "fields": _FIELDS,
            "maxResults": PAGE_SIZE,
        }
        if token:
            params["nextPageToken"] = token
        response = self._client.get("/rest/api/3/search/jql", params=params)
        response.raise_for_status()
        page: dict[str, Any] = response.json()
        return page

    def _to_ticket(self, issue: dict[str, Any]) -> Ticket:
        fields = issue["fields"]
        key = issue["key"]
        labels = list(fields.get("labels") or [])
        # Jira's own classification is a useful hint, so it reaches triage as labels.
        if issue_type := fields.get("issuetype"):
            labels.append(f"jira-type:{issue_type['name']}")
        if priority := fields.get("priority"):
            labels.append(f"jira-priority:{priority['name']}")
        return Ticket(
            ref=TicketRef(source=SOURCE_NAME, project=fields["project"]["key"], key=key),
            title=fields["summary"],
            body=adf_to_text(fields.get("description")),
            author=(fields.get("reporter") or {}).get("displayName", "unknown"),
            labels=labels,
            url=f"{self._base_url}/browse/{key}",
            created_at=_parse_jira_time(fields["created"]),
        )
