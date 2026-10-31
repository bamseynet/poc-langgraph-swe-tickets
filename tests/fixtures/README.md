# Recorded fixtures

Tests never call the network. These files replay real API shapes through
`httpx.MockTransport`.

- `github/issues_page1.json` and `github/comments.json` were recorded from
  `GET /repos/{owner}/{repo}/issues` and `.../issues/{n}/comments` on a public
  repository (2026-10-31), then sanitised: every field GitHub returned is kept, but
  usernames, URLs, titles and bodies were replaced with `octo-org/octo-repo` demo
  content. The list contains one pull request, because the issues API returns those
  too and the source must drop them.
- `jira/search_page1.json` and `jira/search_page2.json` are **hand-written** from
  Atlassian's documented response for `GET /rest/api/3/search/jql`. No Jira instance
  was contacted, so treat the Jira adapter as unverified against a live server.
