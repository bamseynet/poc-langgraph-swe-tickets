"""GitHub Issues: the source tickets come from and the writer approvals go to."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from ticketgate.models import Ticket, TicketRef, WriteBackResult

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
PAGE_SIZE = 50
TIMEOUT_SECONDS = 30.0
SOURCE_NAME = "github"


def github_client(
    token: str | None = None, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=API_URL, headers=headers, timeout=TIMEOUT_SECONDS, transport=transport
    )


def _paginate(
    client: httpx.Client, path: str, params: dict[str, str | int]
) -> Iterator[dict[str, Any]]:
    response = client.get(path, params=params)
    while True:
        response.raise_for_status()
        yield from response.json()
        next_link = response.links.get("next")
        if next_link is None:
            return
        response = client.get(next_link["url"])


class GitHubIssuesSource:
    def __init__(
        self,
        client: httpx.Client,
        repo: str,
        *,
        label_filter: str | None = None,
    ) -> None:
        self._client = client
        self._repo = repo
        self._label_filter = label_filter

    def list_tickets(self) -> list[Ticket]:
        params: dict[str, str | int] = {
            "state": "open",
            "sort": "created",
            "direction": "asc",
            "per_page": PAGE_SIZE,
        }
        if self._label_filter:
            params["labels"] = self._label_filter

        issues = _paginate(self._client, f"/repos/{self._repo}/issues", params)
        # The issues API also returns pull requests.
        return [self._to_ticket(issue) for issue in issues if "pull_request" not in issue]

    def _to_ticket(self, issue: dict[str, Any]) -> Ticket:
        return Ticket(
            ref=TicketRef(source=SOURCE_NAME, project=self._repo, key=str(issue["number"])),
            title=issue["title"],
            body=issue.get("body") or "",
            author=(issue.get("user") or {}).get("login", "unknown"),
            labels=[label["name"] for label in issue.get("labels", [])],
            url=issue["html_url"],
            created_at=issue["created_at"],
        )


def _marker(idempotency_key: str) -> str:
    return f"<!-- ticketgate:decision={idempotency_key} -->"


class GitHubIssuesWriter:
    """Writes one comment and adds labels. It has no code path that touches repository content."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def write_back(
        self, ref: TicketRef, *, comment: str, labels: list[str], idempotency_key: str
    ) -> WriteBackResult:
        issue_path = f"/repos/{ref.project}/issues/{ref.key}"
        marker = _marker(idempotency_key)

        # The key is a random decision id, unknown until approval, so nobody commenting on
        # a public issue can plant a marker that suppresses the real comment.
        comment_url = self._find_comment(issue_path, marker)
        deduplicated = comment_url is not None
        if comment_url is None:
            response = self._client.post(
                f"{issue_path}/comments", json={"body": f"{comment}\n\n{marker}"}
            )
            response.raise_for_status()
            comment_url = response.json()["html_url"]

        if labels:
            # Adding labels is idempotent, and a crash may have landed before this call.
            response = self._client.post(f"{issue_path}/labels", json={"labels": labels})
            response.raise_for_status()

        return WriteBackResult(comment_url=comment_url, labels=labels, deduplicated=deduplicated)

    def _find_comment(self, issue_path: str, marker: str) -> str | None:
        comments = _paginate(self._client, f"{issue_path}/comments", {"per_page": 100})
        for comment in comments:
            if marker in (comment.get("body") or ""):
                return str(comment["html_url"])
        return None
