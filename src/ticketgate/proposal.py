"""Turns agent output into the exact comment and labels a reviewer approves."""

from __future__ import annotations

import re

from ticketgate.models import MAX_COMMENT_CHARS, Investigation, Proposal, TriageResult

MAX_DIFF_CHARS = 40_000
MAX_PATCH_CHARS = 50_000
_FOOTER = "_Drafted by an AI agent and reviewed by a human before posting._"
_TRUNCATED = "\n\n_This text was truncated._"


def _fenced_diff(diff: str) -> str:
    shown = diff
    note = ""
    if len(diff) > MAX_DIFF_CHARS:
        shown = diff[:MAX_DIFF_CHARS]
        note = f"\n\nThe diff was truncated to {MAX_DIFF_CHARS} characters."
    # A diff can contain backticks, so the fence must be longer than any run inside it.
    longest = max((len(run) for run in re.findall(r"`+", shown)), default=0)
    fence = "`" * max(3, longest + 1)
    fenced = f"{fence}diff\n{shown.rstrip()}\n{fence}{note}"
    if len(fenced) > MAX_PATCH_CHARS:
        return "The patch is too large to show here. The reviewer can see it in full."
    return fenced


def _render_investigation(investigation: Investigation) -> tuple[str, str]:
    """Return the text and the patch separately, so only the text is ever cut short."""
    parts: list[str] = []
    if not investigation.complete:
        parts.append("**This investigation is incomplete.**")
    parts.append(investigation.summary)
    if investigation.plan:
        steps = "\n".join(f"{n}. {step}" for n, step in enumerate(investigation.plan, start=1))
        parts.append(f"### Proposed plan\n\n{steps}")
    patch = ""
    if investigation.diff:
        patch = f"\n\n### Proposed patch (not applied)\n\n{_fenced_diff(investigation.diff)}"
    return "\n\n".join(parts), patch


def render_proposal(
    triage: TriageResult,
    *,
    draft_reply: str | None = None,
    investigation: Investigation | None = None,
) -> Proposal:
    text, patch = draft_reply or "", ""
    if investigation is not None:
        text, patch = _render_investigation(investigation)
    room = MAX_COMMENT_CHARS - len(patch) - len(_FOOTER) - 2
    if len(text) > room:
        text = text[: room - len(_TRUNCATED)] + _TRUNCATED
    return Proposal(comment=f"{text}{patch}\n\n{_FOOTER}", labels=triage.labels())
