from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ticketgate.github import GitHubIssuesSource, GitHubIssuesWriter, github_client
from ticketgate.models import TicketRef

FIXTURES = Path(__file__).parent.parent / "fixtures" / "github"
REPO = "octo-org/octo-repo"
REF = TicketRef(source="github", project=REPO, key="101")

Handler = Callable[[httpx.Request], httpx.Response]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _client(handler: Handler, token: str | None = "tok") -> httpx.Client:  # noqa: S107
    return github_client(token=token, transport=httpx.MockTransport(handler))


def _issues(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_fixture("issues_page1.json"))


def test_lists_open_issues_as_normalised_tickets() -> None:
    tickets = GitHubIssuesSource(_client(_issues), REPO).list_tickets()
    first = tickets[0]
    assert first.ref == REF
    assert first.title == "Checkout crashes when the discount is 100%"
    assert "ZeroDivisionError" in first.body
    assert first.author == "octocat"
    assert first.labels == ["needs-triage"]
    assert first.url == "https://github.com/octo-org/octo-repo/issues/101"
    assert first.created_at.tzinfo is not None


def test_drops_pull_requests_because_the_issues_api_returns_them_too() -> None:
    tickets = GitHubIssuesSource(_client(_issues), REPO).list_tickets()
    assert [t.ref.key for t in tickets] == ["101", "102"]


def test_requests_only_open_issues_oldest_first() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return _issues(request)

    GitHubIssuesSource(_client(handler), REPO).list_tickets()
    assert seen[0].path == f"/repos/{REPO}/issues"
    assert seen[0].params["state"] == "open"
    assert seen[0].params["sort"] == "created"
    assert seen[0].params["direction"] == "asc"


def test_label_filter_is_sent_to_the_api() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return _issues(request)

    GitHubIssuesSource(_client(handler), REPO, label_filter="agent-triage").list_tickets()
    assert seen[0].params["labels"] == "agent-triage"


def test_follows_pagination_links() -> None:
    issues = _fixture("issues_page1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[issues[1]])
        next_url = f"https://api.github.com/repos/{REPO}/issues?page=2"
        return httpx.Response(200, json=[issues[0]], headers={"Link": f'<{next_url}>; rel="next"'})

    tickets = GitHubIssuesSource(_client(handler), REPO).list_tickets()
    assert [t.ref.key for t in tickets] == ["101", "102"]


def test_never_follows_a_pagination_link_to_another_host() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        link = '<https://evil.example/steal?page=2>; rel="next"'
        return httpx.Response(200, json=[], headers={"Link": link})

    with pytest.raises(ValueError, match=r"evil\.example"):
        GitHubIssuesSource(_client(handler), REPO).list_tickets()
    assert seen == ["api.github.com"]


def test_null_issue_body_becomes_empty_text() -> None:
    issue = _fixture("issues_page1.json")[0] | {"body": None}
    client = _client(lambda _: httpx.Response(200, json=[issue]))
    assert GitHubIssuesSource(client, REPO).list_tickets()[0].body == ""


def test_source_raises_on_an_api_error() -> None:
    client = _client(lambda _: httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        GitHubIssuesSource(client, REPO).list_tickets()


def test_token_is_sent_as_a_bearer_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _issues(request)

    GitHubIssuesSource(_client(handler, token="tok"), REPO).list_tickets()
    assert seen[0].headers["Authorization"] == "Bearer tok"


class _FakeGitHub:
    """Records writes and serves recorded responses."""

    def __init__(self, existing_comments: list[dict[str, Any]] | None = None) -> None:
        self.comments = existing_comments if existing_comments is not None else []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        comments_path = f"/repos/{REPO}/issues/101/comments"
        labels_path = f"/repos/{REPO}/issues/101/labels"
        if request.method == "GET" and request.url.path == comments_path:
            return httpx.Response(200, json=self.comments)
        if request.method == "POST" and request.url.path == comments_path:
            posted = _fixture("comments.json")[0] | json.loads(request.content)
            self.comments.append(posted)
            return httpx.Response(201, json=posted)
        if request.method == "POST" and request.url.path == labels_path:
            names = json.loads(request.content)["labels"]
            return httpx.Response(200, json=[{"name": n} for n in names])
        return httpx.Response(404, json={"message": "unexpected request"})

    def writes(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests if r.method != "GET"]


def _write(github: _FakeGitHub, key: str = "decision-1") -> Any:
    writer = GitHubIssuesWriter(_client(github))
    return writer.write_back(REF, comment="Proposed plan", labels=["type:bug"], idempotency_key=key)


def test_write_back_posts_the_comment_and_labels() -> None:
    github = _FakeGitHub()
    result = _write(github)
    assert github.writes() == [
        ("POST", f"/repos/{REPO}/issues/101/comments"),
        ("POST", f"/repos/{REPO}/issues/101/labels"),
    ]
    assert github.comments[0]["body"].startswith("Proposed plan")
    assert result.labels == ["type:bug"]
    assert result.comment_url
    assert result.deduplicated is False


def test_write_back_only_ever_touches_comments_and_labels() -> None:
    # The PoC must never push code: no contents, git or pulls endpoints.
    github = _FakeGitHub()
    _write(github)
    for request in github.requests:
        assert request.url.path.endswith(("/comments", "/labels"))


def test_retry_after_a_crash_does_not_post_a_second_comment() -> None:
    github = _FakeGitHub()
    _write(github)
    second = _write(github)
    assert sum(1 for w in github.writes() if w[1].endswith("/comments")) == 1
    assert second.deduplicated is True


def test_retry_still_applies_labels_when_the_comment_was_already_posted() -> None:
    # The crash may have happened between the two POSTs.
    github = _FakeGitHub()
    _write(github)
    github.requests.clear()
    _write(github)
    assert github.writes() == [("POST", f"/repos/{REPO}/issues/101/labels")]


def test_a_different_decision_is_not_treated_as_a_duplicate() -> None:
    github = _FakeGitHub()
    _write(github, key="decision-1")
    _write(github, key="decision-2")
    assert len(github.comments) == 2


def test_empty_labels_skip_the_labels_call() -> None:
    github = _FakeGitHub()
    GitHubIssuesWriter(_client(github)).write_back(REF, comment="c", labels=[], idempotency_key="k")
    assert not any(w[1].endswith("/labels") for w in github.writes())


def test_write_back_raises_when_the_comment_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        GitHubIssuesWriter(_client(handler)).write_back(
            REF, comment="c", labels=[], idempotency_key="k"
        )


def test_never_follows_a_pagination_link_to_another_port() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        link = '<https://api.github.com:8443/x?page=2>; rel="next"'
        return httpx.Response(200, json=[], headers={"Link": link})

    with pytest.raises(ValueError, match="Refusing"):
        GitHubIssuesSource(_client(handler), REPO).list_tickets()
