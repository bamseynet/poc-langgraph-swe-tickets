"""What an API or CLI calls. Hides thread ids, snapshots and `Command` from callers."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel

from ticketgate.graph import DURABILITY, TicketGraph
from ticketgate.models import ApprovalDecision, Ticket
from ticketgate.ports import TicketSource
from ticketgate.store import Store

logger = logging.getLogger(__name__)

ProgressKind = Literal["node", "awaiting_approval", "finished", "failed"]


class TicketStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    WRITTEN_BACK = "written_back"
    REJECTED = "rejected"
    FAILED = "failed"


class TicketNotFound(Exception):
    pass


class TicketConflict(Exception):
    """The ticket is not in a state that allows this action."""


class NotAwaitingApproval(TicketConflict):
    pass


class NotRetryable(TicketConflict):
    pass


@dataclass(frozen=True)
class ProgressEvent:
    thread_id: str
    kind: ProgressKind
    node: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoverResult:
    new: list[str]
    errors: list[str]


class TicketView(BaseModel):
    """A ticket as a caller sees it. Status is read from the checkpoint, not stored apart."""

    thread_id: str
    title: str
    url: str
    status: TicketStatus
    # What the reviewer is being asked to approve, present only while waiting for them.
    request: dict[str, Any] | None = None
    triage: dict[str, Any] | None = None
    draft_reply: str | None = None
    investigation: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    write_back: dict[str, Any] | None = None
    error: str | None = None


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _status_of(snapshot: StateSnapshot) -> TicketStatus:
    if any(task.error for task in snapshot.tasks):
        return TicketStatus.FAILED
    if snapshot.interrupts:
        return TicketStatus.AWAITING_APPROVAL
    # Only an explicit outcome means finished. An empty `next` does not: LangGraph also reports
    # that when a crash lands between saving a node's result and checkpointing the step.
    outcome = snapshot.values.get("outcome")
    if outcome == "written_back":
        return TicketStatus.WRITTEN_BACK
    if outcome == "rejected":
        return TicketStatus.REJECTED
    return TicketStatus.RUNNING if snapshot.values else TicketStatus.QUEUED


def _describe_update(update: Any) -> dict[str, Any]:
    """Pull out of a node update what is worth showing a person watching the run."""
    if not isinstance(update, dict):
        return {}
    for message in update.get("messages", []):
        if isinstance(message, AIMessage) and message.tool_calls:
            return {
                "tool_calls": [{"tool": c["name"], "args": c["args"]} for c in message.tool_calls]
            }
    return {}


class TicketService:
    def __init__(
        self,
        *,
        graph: TicketGraph,
        store: Store,
        sources: Sequence[TicketSource],
        on_progress: Callable[[ProgressEvent], None] | None = None,
        max_new_tickets: int = 50,
    ) -> None:
        self._graph = graph
        self._store = store
        self._sources = list(sources)
        self._on_progress = on_progress or (lambda _event: None)
        self._max_new_tickets = max_new_tickets
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def ingest(self) -> list[str]:
        """Poll every source once and run each new ticket up to the approval gate."""
        new = self.discover().new
        for thread_id in new:
            self.process(thread_id)
        return new

    def discover(self) -> DiscoverResult:
        """Register tickets not seen before, up to the cap, and report unreadable sources.

        The cap limits new work per poll rather than what a source returns. Approved tickets
        stay open, so capping the source would eventually hide every newer ticket.
        """
        new: list[str] = []
        errors: list[str] = []
        for source in self._sources:
            try:
                tickets = source.list_tickets()
            except Exception as exc:
                name = type(source).__name__
                logger.exception("Could not list tickets from %s", name)
                errors.append(f"{name}: {exc}")
                continue
            for ticket in tickets:
                if len(new) >= self._max_new_tickets:
                    break
                thread_id = ticket.ref.thread_id
                if self._store.register_ticket(thread_id, ticket.model_dump(mode="json")):
                    self._store.record_event(
                        event_key=f"{thread_id}:ingested",
                        thread_id=thread_id,
                        actor="system",
                        action="ticket.ingested",
                        detail={"title": ticket.title, "url": ticket.url},
                    )
                    new.append(thread_id)
        return DiscoverResult(new=new, errors=errors)

    def process(self, thread_id: str) -> None:
        """Start a registered ticket, or continue it from its last checkpoint."""
        with self._lock(thread_id):
            self._advance(thread_id)

    def recover(self) -> list[str]:
        """Finish what a previous process left unfinished. Call this once at startup.

        Tickets waiting for a human are left alone: only `decide` can move them on.
        """
        unfinished = {TicketStatus.QUEUED, TicketStatus.RUNNING, TicketStatus.FAILED}
        recovered = [t.thread_id for t in self.list_tickets() if t.status in unfinished]
        for thread_id in recovered:
            self.process(thread_id)
        return recovered

    def list_tickets(self, status: TicketStatus | None = None) -> list[TicketView]:
        views = [self.get(thread_id) for thread_id in self._store.list_thread_ids()]
        return [v for v in views if status is None or v.status is status]

    def get(self, thread_id: str) -> TicketView:
        ticket = self._load_ticket(thread_id)
        snapshot = self._graph.get_state(_config(thread_id))
        values = snapshot.values
        errors = [str(task.error) for task in snapshot.tasks if task.error]
        return TicketView(
            thread_id=thread_id,
            title=ticket.title,
            url=ticket.url,
            status=_status_of(snapshot),
            request=snapshot.interrupts[0].value if snapshot.interrupts else None,
            triage=values.get("triage"),
            draft_reply=values.get("draft_reply"),
            investigation=values.get("investigation"),
            proposal=values.get("proposal"),
            decision=values.get("decision"),
            write_back=values.get("write_back"),
            error=errors[0] if errors else None,
        )

    def decide(self, thread_id: str, decision: ApprovalDecision) -> TicketView:
        """Resume a waiting ticket with a human decision.

        The per-ticket lock means that of two simultaneous approvals, the second sees the
        ticket is no longer waiting. It only works within one process, and `open_app` holds a
        file lock to keep a second process off the same data.
        """
        self._load_ticket(thread_id)
        with self._lock(thread_id):
            if not self._graph.get_state(_config(thread_id)).interrupts:
                raise NotAwaitingApproval(f"{thread_id} is not waiting for approval")
            self._run(thread_id, Command(resume=decision.model_dump(mode="json")))
        return self.get(thread_id)

    def retry(self, thread_id: str) -> TicketView:
        """Continue a failed ticket from its last checkpoint, keeping any human decision."""
        self._load_ticket(thread_id)
        with self._lock(thread_id):
            status = _status_of(self._graph.get_state(_config(thread_id)))
            if status is not TicketStatus.FAILED:
                raise NotRetryable(f"{thread_id} is {status.value}, not failed")
            self._advance(thread_id)
        return self.get(thread_id)

    def _lock(self, thread_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[thread_id]

    def _load_ticket(self, thread_id: str) -> Ticket:
        raw = self._store.get_ticket(thread_id)
        if raw is None:
            raise TicketNotFound(thread_id)
        return Ticket.model_validate(raw)

    def _advance(self, thread_id: str) -> None:
        snapshot = self._graph.get_state(_config(thread_id))
        status = _status_of(snapshot)
        if status is TicketStatus.QUEUED:
            self._run(thread_id, {"ticket": self._load_ticket(thread_id).model_dump(mode="json")})
        elif status in (TicketStatus.RUNNING, TicketStatus.FAILED):
            # None carries on from the checkpoint. This branch must stay out of reach for a
            # ticket waiting for a human, because None would consume a saved resume value.
            self._run(thread_id, None)

    def _run(self, thread_id: str, graph_input: Any) -> None:
        try:
            for _namespace, update in self._graph.stream(
                graph_input,
                _config(thread_id),
                stream_mode="updates",
                subgraphs=True,
                durability=DURABILITY,
            ):
                for node, node_update in cast(dict[str, Any], update).items():
                    if not node.startswith("__"):
                        self._emit(
                            ProgressEvent(thread_id, "node", node, _describe_update(node_update))
                        )
        except Exception as exc:
            # The failure stays on the thread, so the ticket shows as failed and can be retried.
            logger.exception("Run failed for %s", thread_id)
            self._store.record_event(
                event_key=f"{thread_id}:run.failed:{uuid.uuid4().hex}",
                thread_id=thread_id,
                actor="system",
                action="run.failed",
                detail={"error": str(exc)},
            )
            self._emit(ProgressEvent(thread_id, "failed", detail={"error": str(exc)}))
            return

        snapshot = self._graph.get_state(_config(thread_id))
        kind: ProgressKind = "awaiting_approval" if snapshot.interrupts else "finished"
        self._emit(ProgressEvent(thread_id, kind))

    def _emit(self, event: ProgressEvent) -> None:
        # Progress is a courtesy. A broken listener must never stop a ticket mid-run.
        try:
            self._on_progress(event)
        except Exception:
            logger.exception("Progress listener failed for %s", event.thread_id)
