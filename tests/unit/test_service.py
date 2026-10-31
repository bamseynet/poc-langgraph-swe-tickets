from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel

from tests.support import SpyWriter, make_deps, make_ticket, scripted, submit, triage
from ticketgate.graph import GraphDeps, build_graph, open_checkpointer
from ticketgate.models import ApprovalDecision, Ticket
from ticketgate.service import (
    NotAwaitingApproval,
    NotRetryable,
    ProgressEvent,
    TicketNotFound,
    TicketService,
    TicketStatus,
)


class ListSource:
    def __init__(self, *tickets: Ticket) -> None:
        self.tickets = list(tickets)

    def list_tickets(self) -> list[Ticket]:
        return self.tickets


class Harness:
    def __init__(self, tmp_path: Path, checkpointer: Any) -> None:
        self.tmp_path = tmp_path
        self.checkpointer = checkpointer
        self.writer = SpyWriter()
        self.events: list[ProgressEvent] = []

    def service(
        self,
        *tickets: Ticket,
        worker: BaseChatModel | None = None,
        principal: Any = None,
        extra_sources: tuple[Any, ...] = (),
    ) -> TicketService:
        self.deps: GraphDeps = make_deps(
            self.tmp_path,
            worker=worker or scripted(),
            principal=principal or scripted(),
            writer=self.writer,
        )
        graph = build_graph(self.deps, self.checkpointer)
        return TicketService(
            graph=graph,
            store=self.deps.store,
            sources=[*extra_sources, ListSource(*tickets)],
            on_progress=self.events.append,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with open_checkpointer(tmp_path / "app.db") as checkpointer:
        yield Harness(tmp_path, checkpointer)


THREAD = make_ticket().ref.thread_id


def _approve() -> ApprovalDecision:
    return ApprovalDecision(action="approve", actor="alice")


def _ingested_bug(harness: Harness) -> TicketService:
    service = harness.service(
        make_ticket(), worker=scripted(triage("bug")), principal=scripted(submit())
    )
    service.ingest()
    return service


def test_ingest_runs_each_new_ticket_to_the_gate(harness: Harness) -> None:
    service = _ingested_bug(harness)
    view = service.get(THREAD)
    assert view.status is TicketStatus.AWAITING_APPROVAL
    assert view.title == "Totals are wrong"
    assert view.request is not None
    assert view.request["triage"]["type"] == "bug"
    assert harness.writer.calls == []


def test_polling_again_does_not_reprocess_a_ticket(harness: Harness) -> None:
    # A second run would call the model again and raise, because the script is empty.
    service = _ingested_bug(harness)
    assert service.ingest() == []
    assert len(service.list_tickets()) == 1


def test_records_ingestion_in_the_audit_log(harness: Harness) -> None:
    _ingested_bug(harness)
    events = harness.deps.store.list_events(thread_id=THREAD)
    assert events[-1].action == "ticket.ingested"
    assert events[-1].actor == "system"


def test_one_failing_source_does_not_stop_the_others(harness: Harness) -> None:
    class BrokenSource:
        def list_tickets(self) -> list[Ticket]:
            raise httpx.ConnectError("source down")

    service = harness.service(
        make_ticket(),
        worker=scripted(triage("bug")),
        principal=scripted(submit()),
        extra_sources=(BrokenSource(),),
    )
    assert service.ingest() == [THREAD]


def test_lists_the_queue_of_pending_approvals(harness: Harness) -> None:
    service = _ingested_bug(harness)
    assert [t.thread_id for t in service.list_tickets(TicketStatus.AWAITING_APPROVAL)] == [THREAD]
    assert service.list_tickets(TicketStatus.WRITTEN_BACK) == []


def test_decide_approve_writes_and_finishes(harness: Harness) -> None:
    service = _ingested_bug(harness)
    view = service.decide(THREAD, _approve())
    assert view.status is TicketStatus.WRITTEN_BACK
    assert view.request is None
    assert view.write_back is not None
    assert len(harness.writer.calls) == 1


def test_decide_reject_finishes_without_writing(harness: Harness) -> None:
    service = _ingested_bug(harness)
    view = service.decide(
        THREAD, ApprovalDecision(action="reject", actor="bob", reason="Not a bug")
    )
    assert view.status is TicketStatus.REJECTED
    assert view.decision is not None
    assert view.decision["reason"] == "Not a bug"
    assert harness.writer.calls == []


def test_a_second_decision_is_refused(harness: Harness) -> None:
    service = _ingested_bug(harness)
    service.decide(THREAD, _approve())
    with pytest.raises(NotAwaitingApproval):
        service.decide(THREAD, _approve())
    assert len(harness.writer.calls) == 1


def test_concurrent_approvals_write_only_once(harness: Harness) -> None:
    service = _ingested_bug(harness)

    def attempt(_: int) -> str:
        try:
            service.decide(THREAD, _approve())
        except NotAwaitingApproval:
            return "refused"
        return "accepted"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert results.count("accepted") == 1
    assert len(harness.writer.calls) == 1


def test_deciding_an_unknown_ticket_fails(harness: Harness) -> None:
    service = harness.service()
    with pytest.raises(TicketNotFound):
        service.decide("nope", _approve())
    with pytest.raises(TicketNotFound):
        service.get("nope")


def test_model_failure_shows_as_failed_without_losing_the_ticket(harness: Harness) -> None:
    service = harness.service(
        make_ticket(),
        worker=scripted(ConnectionError("throttled"), triage("bug")),
        principal=scripted(submit()),
    )
    service.ingest()
    view = service.get(THREAD)
    assert view.status is TicketStatus.FAILED
    assert view.error is not None
    assert "throttled" in view.error

    assert service.retry(THREAD).status is TicketStatus.AWAITING_APPROVAL


def test_failed_write_back_can_be_retried_without_a_new_decision(harness: Harness) -> None:
    harness.writer.failures.append(httpx.ConnectError("GitHub is down"))
    service = _ingested_bug(harness)
    decision = _approve()

    view = service.decide(THREAD, decision)
    assert view.status is TicketStatus.FAILED
    assert harness.writer.calls == []
    failed = [e for e in harness.deps.store.list_events() if e.action == "run.failed"]
    assert "GitHub is down" in failed[0].detail["error"]

    view = service.retry(THREAD)
    assert view.status is TicketStatus.WRITTEN_BACK
    assert harness.writer.calls[0].idempotency_key == decision.decision_id


def test_retry_refuses_a_ticket_waiting_for_a_human(harness: Harness) -> None:
    # Retrying must never look like an approval.
    service = _ingested_bug(harness)
    with pytest.raises(NotRetryable):
        service.retry(THREAD)
    assert harness.writer.calls == []
    assert service.get(THREAD).status is TicketStatus.AWAITING_APPROVAL


def test_progress_events_report_each_step(harness: Harness) -> None:
    _ingested_bug(harness)
    nodes = [e.node for e in harness.events if e.kind == "node"]
    assert "triage" in nodes
    assert "agent" in nodes
    assert "prepare_proposal" in nodes
    assert harness.events[-1].kind == "awaiting_approval"
    assert all(e.thread_id == THREAD for e in harness.events)


def test_recover_finishes_tickets_a_crash_left_unfinished(harness: Harness) -> None:
    # Registered, but the process died before the graph ever started.
    service = harness.service(worker=scripted(triage("bug")), principal=scripted(submit()))
    ticket = make_ticket()
    harness.deps.store.register_ticket(THREAD, ticket.model_dump(mode="json"))
    assert service.get(THREAD).status is TicketStatus.QUEUED

    assert service.recover() == [THREAD]

    assert service.get(THREAD).status is TicketStatus.AWAITING_APPROVAL


def test_recover_leaves_tickets_waiting_for_a_human_alone(harness: Harness) -> None:
    service = _ingested_bug(harness)
    assert service.recover() == []
    assert harness.writer.calls == []
    assert service.get(THREAD).status is TicketStatus.AWAITING_APPROVAL


def test_recover_retries_a_failed_write_back_after_a_restart(harness: Harness) -> None:
    harness.writer.failures.append(httpx.ConnectError("down"))
    service = _ingested_bug(harness)
    service.decide(THREAD, _approve())
    assert service.recover() == [THREAD]
    assert len(harness.writer.calls) == 1
