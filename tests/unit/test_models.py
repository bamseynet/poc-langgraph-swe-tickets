from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ticketgate.models import ApprovalDecision, Ticket, TicketRef, TriageResult


def _triage(**overrides: object) -> TriageResult:
    data: dict[str, object] = {
        "type": "bug",
        "severity": "high",
        "component": "billing",
        "rationale": "crash",
    }
    return TriageResult.model_validate(data | overrides)


def test_thread_id_is_stable_for_a_ticket() -> None:
    ref = TicketRef(source="github", project="octo-org/octo-repo", key="101")
    assert ref.thread_id == "github:octo-org/octo-repo#101"


def test_ticket_round_trips_through_plain_json() -> None:
    # Graph state only holds JSON, so a ticket must survive the trip.
    ticket = Ticket(
        ref=TicketRef(source="demo", project="p", key="1"),
        title="t",
        body="b",
        author="a",
        url="https://example.test/1",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert Ticket.model_validate(ticket.model_dump(mode="json")) == ticket


def test_triage_rejects_an_unknown_type() -> None:
    with pytest.raises(ValidationError):
        _triage(type="incident")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Billing", "billing"),
        ("Payment Gateway", "payment-gateway"),
        ("  api/v2  ", "api-v2"),
        ("", "unknown"),
        ("!!!", "unknown"),
        ("x" * 80, "x" * 30),
    ],
)
def test_triage_normalises_component_to_a_label_safe_slug(raw: str, expected: str) -> None:
    assert _triage(component=raw).component == expected


def test_triage_labels_encode_type_severity_and_component() -> None:
    assert _triage().labels() == ["type:bug", "severity:high", "component:billing"]


def test_triage_schema_avoids_constraints_bedrock_structured_output_rejects() -> None:
    # Bedrock returns a 400 for minLength/maxLength in a structured-output schema.
    schema = str(TriageResult.model_json_schema())
    assert "maxLength" not in schema
    assert "minLength" not in schema


def test_approve_needs_only_an_actor() -> None:
    decision = ApprovalDecision(action="approve", actor="alice")
    assert decision.decision_id
    assert decision.decided_at.tzinfo is not None


def test_each_decision_gets_a_unique_id() -> None:
    first = ApprovalDecision(action="approve", actor="alice")
    second = ApprovalDecision(action="approve", actor="alice")
    assert first.decision_id != second.decision_id


@pytest.mark.parametrize("actor", ["", "   "])
def test_decision_requires_an_actor(actor: str) -> None:
    with pytest.raises(ValidationError, match="actor"):
        ApprovalDecision(action="approve", actor=actor)


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_reject_requires_a_reason(reason: str | None) -> None:
    with pytest.raises(ValidationError, match="reason"):
        ApprovalDecision(action="reject", actor="alice", reason=reason)


def test_edit_requires_an_edited_field() -> None:
    with pytest.raises(ValidationError, match="edit"):
        ApprovalDecision(action="edit", actor="alice")


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_only_edit_may_carry_edits(action: str) -> None:
    with pytest.raises(ValidationError, match="edit"):
        ApprovalDecision.model_validate(
            {"action": action, "actor": "a", "reason": "r", "edited_comment": "changed"}
        )


def test_edited_comment_must_fit_in_a_github_comment() -> None:
    with pytest.raises(ValidationError, match="60000"):
        ApprovalDecision(action="edit", actor="alice", edited_comment="x" * 60_001)


def test_edit_may_clear_all_labels() -> None:
    decision = ApprovalDecision(action="edit", actor="alice", edited_labels=[])
    assert decision.edited_labels == []


@pytest.mark.parametrize("comment", ["", "   \n"])
def test_edit_cannot_blank_the_comment(comment: str) -> None:
    with pytest.raises(ValidationError, match="edited_comment"):
        ApprovalDecision(action="edit", actor="alice", edited_comment=comment)
