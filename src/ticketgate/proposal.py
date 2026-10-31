"""Turns agent output into the exact comment and labels a reviewer approves."""

from __future__ import annotations

import re

from ticketgate.models import MAX_COMMENT_CHARS, Investigation, Proposal, TriageResult

MAX_DIFF_CHARS = 40_000
_FOOTER = "_Drafted by an AI agent and reviewed by a human before posting._"


def _fenced_diff(diff: str) -> str:
    shown = diff
    note = ""
    if len(diff) > MAX_DIFF_CHARS:
        shown = diff[:MAX_DIFF_CHARS]
        note = f"\n\nThe diff was truncated to {MAX_DIFF_CHARS} characters."
    # A diff can contain backticks, so the fence must be longer than any run inside it.
    longest = max((len(run) for run in re.findall(r"`+", shown)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}diff\n{shown.rstrip()}\n{fence}{note}"


def _render_investigation(investigation: Investigation) -> str:
    parts: list[str] = []
    if not investigation.complete:
        parts.append("**This investigation is incomplete.**")
    parts.append(investigation.summary)
    if investigation.plan:
        steps = "\n".join(f"{n}. {step}" for n, step in enumerate(investigation.plan, start=1))
        parts.append(f"### Proposed plan\n\n{steps}")
    if investigation.diff:
        parts.append(f"### Proposed patch (not applied)\n\n{_fenced_diff(investigation.diff)}")
    return "\n\n".join(parts)


def render_proposal(
    triage: TriageResult,
    *,
    draft_reply: str | None = None,
    investigation: Investigation | None = None,
) -> Proposal:
    body = draft_reply or ""
    if investigation is not None:
        body = _render_investigation(investigation)
    room = MAX_COMMENT_CHARS - len(_FOOTER) - 2
    return Proposal(comment=f"{body[:room]}\n\n{_FOOTER}", labels=triage.labels())
