"""LangGraph state. Everything here is JSON, see `ticketgate.models`."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

Outcome = Literal["written_back", "rejected"]


class TicketState(TypedDict, total=False):
    ticket: dict[str, Any]
    triage: dict[str, Any]
    draft_reply: str
    investigation: dict[str, Any]
    proposal: dict[str, Any]
    decision: dict[str, Any]
    write_back: dict[str, Any]
    outcome: Outcome


class InvestigatorState(TypedDict, total=False):
    """Subgraph state. Only keys shared with `TicketState` reach the parent, so the
    conversation with the model stays out of the approval payload."""

    ticket: dict[str, Any]
    triage: dict[str, Any]
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    investigation: dict[str, Any]
