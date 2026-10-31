"""The ticket workflow, and the human-in-the-loop approval gate at its centre.

    triage -> draft_reply | investigate -> prepare_proposal -> approval_gate
    approval_gate -> record_decision -> write_back | record_rejection

`approval_gate` calls `interrupt()`, which stops the run and saves it to the checkpointer.
The thread then waits, for as long as it takes and across restarts, until someone calls
`graph.invoke(Command(resume=decision), config)`. `write_back` comes after the gate, and it
is the only node that uses the writer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy, interrupt
from pydantic import ValidationError

from ticketgate.investigator import build_investigator
from ticketgate.llm import Llms
from ticketgate.models import (
    ApprovalDecision,
    Investigation,
    Proposal,
    Ticket,
    TriageResult,
)
from ticketgate.ports import TicketWriter
from ticketgate.prompts import DRAFT_REPLY_SYSTEM, TRIAGE_SYSTEM, render_ticket
from ticketgate.proposal import render_proposal
from ticketgate.routing import route_after_decision, route_after_triage
from ticketgate.state import TicketState
from ticketgate.store import Store

GATE_NODE = "approval_gate"
RECORD_NODE = "record_decision"
WRITE_NODE = "write_back"

# Save each step before starting the next. The default, "async", can lose the latest step in
# a crash, which could lose a human's decision.
DURABILITY: Literal["sync"] = "sync"

# Bad model output (an empty reply, JSON that does not parse) raises ValueError, which
# LangGraph's default policy never retries. Retry everything except programming errors.
DEFAULT_RETRY = RetryPolicy(
    max_attempts=3, retry_on=lambda exc: not isinstance(exc, TypeError | KeyError)
)

TicketGraph = CompiledStateGraph[TicketState, Any, TicketState, TicketState]


@dataclass(frozen=True)
class GraphDeps:
    llms: Llms
    writer: TicketWriter
    store: Store
    target_repo: Path
    max_investigation_steps: int = 12
    retry: RetryPolicy = field(default_factory=lambda: DEFAULT_RETRY)


@contextmanager
def open_checkpointer(path: Path | str) -> Iterator[SqliteSaver]:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        # State holds JSON and LangChain messages, so nothing else needs deserialising.
        # Allowing nothing more means a tampered database cannot construct arbitrary objects.
        serde = JsonPlusSerializer(allowed_msgpack_modules=None)
        saver = SqliteSaver(conn, serde=serde)
        saver.setup()
        yield saver
    finally:
        conn.close()


class _Nodes:
    def __init__(self, deps: GraphDeps) -> None:
        self._deps = deps

    def triage(self, state: TicketState) -> TicketState:
        ticket = Ticket.model_validate(state["ticket"])
        classifier = self._deps.llms.worker.with_structured_output(
            TriageResult, method="json_schema"
        )
        result = TriageResult.model_validate(
            classifier.invoke(
                [
                    SystemMessage(content=TRIAGE_SYSTEM),
                    HumanMessage(content=render_ticket(ticket)),
                ]
            )
        )
        thread_id = ticket.ref.thread_id
        self._deps.store.record_event(
            event_key=f"{thread_id}:triage",
            thread_id=thread_id,
            actor="agent:triage",
            action="triage.classified",
            detail=result.model_dump(mode="json"),
        )
        return {"triage": result.model_dump(mode="json")}

    def draft_reply(self, state: TicketState) -> TicketState:
        ticket = Ticket.model_validate(state["ticket"])
        reply = self._deps.llms.worker.invoke(
            [
                SystemMessage(content=DRAFT_REPLY_SYSTEM),
                HumanMessage(content=render_ticket(ticket)),
            ]
        )
        text = str(reply.text)
        if not text.strip():
            raise ValueError("The model returned an empty reply")
        return {"draft_reply": text}

    def prepare_proposal(self, state: TicketState) -> TicketState:
        raw = state.get("investigation")
        proposal = render_proposal(
            TriageResult.model_validate(state["triage"]),
            draft_reply=state.get("draft_reply"),
            investigation=Investigation.model_validate(raw) if raw else None,
        )
        thread_id = Ticket.model_validate(state["ticket"]).ref.thread_id
        self._deps.store.record_event(
            event_key=f"{thread_id}:approval.requested",
            thread_id=thread_id,
            actor="system",
            action="approval.requested",
            detail={},
        )
        return {"proposal": proposal.model_dump(mode="json")}

    def approval_gate(self, state: TicketState) -> TicketState:
        """Stop until a human decides.

        LangGraph re-runs a node from its first line on resume, and `interrupt()` then returns
        the resume value. If this node raised after that, LangGraph would keep the resume value
        and replay it over any later decision, so a rejection could lose to an old approval.
        So this node does nothing that can fail: side effects belong to `record_decision`, and
        an invalid value simply asks again.
        """
        request = {
            "ticket": state["ticket"],
            "triage": state["triage"],
            "draft_reply": state.get("draft_reply"),
            "investigation": state.get("investigation"),
            "proposal": state["proposal"],
        }
        while True:
            resumed = interrupt(request)
            try:
                decision = ApprovalDecision.model_validate(resumed)
            except ValidationError as exc:
                request = {**request, "invalid_decision": str(exc)}
            else:
                return {"decision": decision.model_dump(mode="json")}

    def record_decision(self, state: TicketState) -> TicketState:
        """Audit the decision. It runs after the gate's checkpoint, so it can safely retry."""
        thread_id = Ticket.model_validate(state["ticket"]).ref.thread_id
        decision = ApprovalDecision.model_validate(state["decision"])
        proposal = Proposal.model_validate(state["proposal"])
        final_comment, final_labels = _final_write_back(proposal, decision)
        self._deps.store.record_event(
            event_key=f"{thread_id}:decision:{decision.decision_id}",
            thread_id=thread_id,
            actor=decision.actor,
            action=f"decision.{decision.action}",
            detail={
                "decision_id": decision.decision_id,
                "decided_at": decision.decided_at.isoformat(),
                "reason": decision.reason,
                "proposed_comment": proposal.comment,
                "proposed_labels": proposal.labels,
                "final_comment": None if decision.action == "reject" else final_comment,
                "final_labels": None if decision.action == "reject" else final_labels,
            },
        )
        return {}

    def write_back(self, state: TicketState) -> TicketState:
        ref = Ticket.model_validate(state["ticket"]).ref
        decision = ApprovalDecision.model_validate(state["decision"])
        comment, labels = _final_write_back(Proposal.model_validate(state["proposal"]), decision)
        # The decision id makes this safe to repeat if a crash interrupts it, and it stays the
        # same on replay because it was saved with the decision.
        result = self._deps.writer.write_back(
            ref, comment=comment, labels=labels, idempotency_key=decision.decision_id
        )
        self._deps.store.record_event(
            event_key=f"{ref.thread_id}:write_back:{decision.decision_id}",
            thread_id=ref.thread_id,
            actor="system",
            action="write_back.completed",
            detail=result.model_dump(mode="json"),
        )
        return {"write_back": result.model_dump(mode="json"), "outcome": "written_back"}

    def record_rejection(self, state: TicketState) -> TicketState:
        # The reason is already in the audit log and in `decision`. Nothing leaves the system.
        return {"outcome": "rejected"}


def _final_write_back(proposal: Proposal, decision: ApprovalDecision) -> tuple[str, list[str]]:
    comment = proposal.comment if decision.edited_comment is None else decision.edited_comment
    labels = proposal.labels if decision.edited_labels is None else decision.edited_labels
    return comment, labels


def build_graph(deps: GraphDeps, checkpointer: SqliteSaver) -> TicketGraph:
    nodes = _Nodes(deps)
    investigator = build_investigator(
        deps.llms.principal,
        deps.target_repo,
        max_steps=deps.max_investigation_steps,
        retry=deps.retry,
    )

    builder = StateGraph(TicketState)
    builder.add_node("triage", nodes.triage, retry_policy=deps.retry)
    builder.add_node("draft_reply", nodes.draft_reply, retry_policy=deps.retry)
    builder.add_node("investigate", investigator)
    builder.add_node("prepare_proposal", nodes.prepare_proposal)
    builder.add_node(GATE_NODE, nodes.approval_gate)
    builder.add_node(RECORD_NODE, nodes.record_decision, retry_policy=deps.retry)
    builder.add_node(WRITE_NODE, nodes.write_back, retry_policy=deps.retry)
    builder.add_node("record_rejection", nodes.record_rejection)

    builder.add_edge(START, "triage")
    builder.add_conditional_edges("triage", route_after_triage, ["draft_reply", "investigate"])
    builder.add_edge("draft_reply", "prepare_proposal")
    builder.add_edge("investigate", "prepare_proposal")
    builder.add_edge("prepare_proposal", GATE_NODE)
    builder.add_edge(GATE_NODE, RECORD_NODE)
    builder.add_conditional_edges(
        RECORD_NODE, route_after_decision, [WRITE_NODE, "record_rejection"]
    )
    builder.add_edge(WRITE_NODE, END)
    builder.add_edge("record_rejection", END)
    return builder.compile(checkpointer=checkpointer)
