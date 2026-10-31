"""Conditional edges of the graph, as pure functions so they can be tested directly."""

from __future__ import annotations

from typing import Literal

from ticketgate.state import TicketState

TriageRoute = Literal["draft_reply", "investigate"]
DecisionRoute = Literal["write_back", "record_rejection"]

# Questions get an answer. Bugs and features need the codebase read. Chores are not worth
# an investigation, so they get a short acknowledgement and labels for a human to approve.
_TRIAGE_ROUTES: dict[str, TriageRoute] = {
    "question": "draft_reply",
    "chore": "draft_reply",
    "bug": "investigate",
    "feature": "investigate",
}


def route_after_triage(state: TicketState) -> TriageRoute:
    return _TRIAGE_ROUTES[state["triage"]["type"]]


def route_after_decision(state: TicketState) -> DecisionRoute:
    # Fails closed: only an explicit approval or edit reaches the writer.
    if state["decision"]["action"] in ("approve", "edit"):
        return "write_back"
    return "record_rejection"
