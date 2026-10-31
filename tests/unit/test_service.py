from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
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
    WriteBackUnsupported,
    _status_of,
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
        writable_sources: frozenset[str] | None = None,
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
            writable_sources=writable_sources,
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


def test_approving_a_ticket_from_a_source_with_no_writer_is_refused_but_reject_works(
    harness: Harness,
) -> None:
    service = harness.service(
        make_ticket(),
        worker=scripted(triage("bug")),
        principal=scripted(submit()),
        writable_sources=frozenset({"jira"}),
    )
    service.ingest()
    with pytest.raises(WriteBackUnsupported):
        service.decide(THREAD, _approve())
    assert service.get(THREAD).status is TicketStatus.AWAITING_APPROVAL
    assert harness.writer.calls == []
    reject = ApprovalDecision(action="reject", actor="bob", reason="No Jira writer")
    assert service.decide(THREAD, reject).status is TicketStatus.REJECTED


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


def test_recover_retries_a_failed_write_back(harness: Harness) -> None:
    harness.writer.failures.append(httpx.ConnectError("down"))
    service = _ingested_bug(harness)
    service.decide(THREAD, _approve())
    assert service.recover() == [THREAD]
    assert len(harness.writer.calls) == 1


def test_a_raising_progress_listener_cannot_abort_a_run(harness: Harness) -> None:
    def explode(_event: ProgressEvent) -> None:
        raise RuntimeError("client went away")

    deps = make_deps(
        harness.tmp_path,
        worker=scripted(triage("bug")),
        principal=scripted(submit()),
        writer=harness.writer,
    )
    service = TicketService(
        graph=build_graph(deps, harness.checkpointer),
        store=deps.store,
        sources=[ListSource(make_ticket())],
        on_progress=explode,
    )
    service.ingest()
    view = service.decide(THREAD, _approve())
    assert view.status is TicketStatus.WRITTEN_BACK
    assert len(harness.writer.calls) == 1


def test_status_is_written_back_only_when_the_graph_says_so() -> None:
    # A finished-looking snapshot without an outcome has not written anything.
    snapshot = SimpleNamespace(interrupts=(), tasks=(), values={"ticket": {}}, next=())
    assert _status_of(snapshot) is TicketStatus.RUNNING  # type: ignore[arg-type]
    done = SimpleNamespace(interrupts=(), tasks=(), values={"outcome": "written_back"}, next=())
    assert _status_of(done) is TicketStatus.WRITTEN_BACK  # type: ignore[arg-type]
    rejected = SimpleNamespace(interrupts=(), tasks=(), values={"outcome": "rejected"}, next=())
    assert _status_of(rejected) is TicketStatus.REJECTED  # type: ignore[arg-type]


def test_a_task_error_outranks_a_pending_interrupt() -> None:
    snapshot = SimpleNamespace(
        interrupts=("waiting",), tasks=(SimpleNamespace(error="boom"),), values={"t": 1}, next=()
    )
    assert _status_of(snapshot) is TicketStatus.FAILED  # type: ignore[arg-type]


# --- Finding 8: the cap must limit new work, not hide new tickets.


def test_cap_limits_new_tickets_per_poll_and_later_polls_reach_the_rest(harness: Harness) -> None:
    deps = make_deps(harness.tmp_path, worker=scripted(), principal=scripted())
    service = TicketService(
        graph=build_graph(deps, harness.checkpointer),
        store=deps.store,
        sources=[ListSource(make_ticket("1"), make_ticket("2"), make_ticket("3"))],
        max_new_tickets=2,
    )
    assert len(service.discover().new) == 2
    third = service.discover().new
    assert len(third) == 1
    assert third[0].endswith("#3")


def _fail_next_checkpoint(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    real_put = harness.checkpointer.put

    def failing_put(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(harness.checkpointer, "put", real_put)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(harness.checkpointer, "put", failing_put)


def test_a_failure_outside_any_task_shows_as_failed_and_can_be_retried(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _ingested_bug(harness)
    _fail_next_checkpoint(harness, monkeypatch)

    view = service.decide(THREAD, _approve())

    assert view.status is TicketStatus.FAILED
    assert view.error is not None
    assert "database is locked" in view.error
    assert harness.writer.calls == []

    assert service.retry(THREAD).status is TicketStatus.WRITTEN_BACK
    assert len(harness.writer.calls) == 1


def test_retrying_clears_the_recorded_failure(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _ingested_bug(harness)
    _fail_next_checkpoint(harness, monkeypatch)
    service.decide(THREAD, _approve())
    assert service.retry(THREAD).error is None


def test_decide_reports_failure_rather_than_raising_when_the_audit_is_down_too(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The decision is saved by then. Raising would tell the reviewer it had not been.
    service = _ingested_bug(harness)
    store = harness.deps.store
    real_record = store.record_event

    def locked(**_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_event", locked)
    view = service.decide(THREAD, _approve())
    monkeypatch.setattr(store, "record_event", real_record)

    assert view.status is TicketStatus.FAILED
    assert view.decision is not None
    assert harness.writer.calls == []
    with pytest.raises(NotAwaitingApproval):
        service.decide(THREAD, ApprovalDecision(action="reject", actor="bob", reason="No"))


def test_once_the_cap_is_reached_later_sources_are_not_listed(harness: Harness) -> None:
    class Exploding:
        def list_tickets(self) -> list[Ticket]:
            raise AssertionError("listed a source after the cap was reached")

    deps = make_deps(harness.tmp_path, worker=scripted(), principal=scripted())
    service = TicketService(
        graph=build_graph(deps, harness.checkpointer),
        store=deps.store,
        sources=[ListSource(make_ticket("1")), Exploding()],
        max_new_tickets=1,
    )
    assert service.discover().errors == []


def test_discover_reports_a_failing_source(harness: Harness) -> None:
    class BrokenSource:
        def list_tickets(self) -> list[Ticket]:
            raise httpx.ConnectError("source down")

    service = harness.service(extra_sources=(BrokenSource(),))
    result = service.discover()
    assert result.new == []
    assert "BrokenSource" in result.errors[0]
    assert "source down" in result.errors[0]


def test_unfinished_lists_only_tickets_a_restart_should_continue(harness: Harness) -> None:
    service = _ingested_bug(harness)
    assert service.unfinished() == []
    harness.deps.store.register_ticket("queued", make_ticket("9").model_dump(mode="json"))
    assert service.unfinished() == ["queued"]
