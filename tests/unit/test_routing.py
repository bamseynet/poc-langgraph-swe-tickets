from __future__ import annotations

from typing import Any

import pytest

from ticketgate.routing import route_after_decision, route_after_triage


@pytest.mark.parametrize(
    ("ticket_type", "expected"),
    [
        ("question", "draft_reply"),
        ("chore", "draft_reply"),
        ("bug", "investigate"),
        ("feature", "investigate"),
    ],
)
def test_route_after_triage(ticket_type: str, expected: str) -> None:
    state: dict[str, Any] = {"triage": {"type": ticket_type}}
    assert route_after_triage(state) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("action", "expected"),
    [("approve", "write_back"), ("edit", "write_back"), ("reject", "record_rejection")],
)
def test_route_after_decision(action: str, expected: str) -> None:
    state: dict[str, Any] = {"decision": {"action": action}}
    assert route_after_decision(state) == expected  # type: ignore[arg-type]


def test_unknown_decision_never_routes_to_write_back() -> None:
    state: dict[str, Any] = {"decision": {"action": "maybe"}}
    assert route_after_decision(state) == "record_rejection"  # type: ignore[arg-type]
