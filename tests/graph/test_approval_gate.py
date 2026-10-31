"""The pattern itself: nothing is written until a human says so."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, RetryPolicy

from tests.support import (
    GOOD_PATCH,
    SpyWriter,
    make_deps,
    make_ticket,
    scripted,
    submit,
    tool_call,
    triage,
)
from ticketgate.graph import DURABILITY, GATE_NODE, WRITE_NODE, build_graph, open_checkpointer
from ticketgate.models import ApprovalDecision

CONFIG: Any = {"configurable": {"thread_id": "github:octo-org/octo-repo#101"}}


class Harness:
    def __init__(self, tmp_path: Path, checkpointer: SqliteSaver) -> None:
        self.tmp_path = tmp_path
        self.checkpointer = checkpointer
        self.writer = SpyWriter()

    def graph(self, *worker: Any, principal: tuple[Any, ...] = ()) -> CompiledStateGraph[Any]:
        self.deps = make_deps(
            self.tmp_path,
            worker=scripted(*worker),
            principal=scripted(*principal),
            writer=self.writer,
        )
        return build_graph(self.deps, self.checkpointer)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with open_checkpointer(tmp_path / "app.db") as checkpointer:
        yield Harness(tmp_path, checkpointer)


def _start(graph: CompiledStateGraph[Any]) -> dict[str, Any]:
    state = {"ticket": make_ticket().model_dump(mode="json")}
    result: dict[str, Any] = graph.invoke(state, CONFIG, durability=DURABILITY)
    return result


def _resume(graph: CompiledStateGraph[Any], decision: ApprovalDecision) -> dict[str, Any]:
    resume: Command[Any] = Command(resume=decision.model_dump(mode="json"))
    result: dict[str, Any] = graph.invoke(resume, CONFIG, durability=DURABILITY)
    return result


def _bug_graph(harness: Harness, **submission: Any) -> CompiledStateGraph[Any]:
    return harness.graph(triage("bug"), principal=(submit(**submission),))


def _question_graph(harness: Harness) -> CompiledStateGraph[Any]:
    return harness.graph(triage("question", "low"), "Set DEFAULT_CURRENCY.")


def _interrupt_value(graph: CompiledStateGraph[Any]) -> dict[str, Any]:
    interrupts = graph.get_state(CONFIG).interrupts
    assert len(interrupts) == 1
    value: dict[str, Any] = interrupts[0].value
    return value


@pytest.mark.parametrize(
    ("ticket_type", "worker", "principal", "expected_key", "absent_key"),
    [
        ("bug", (), (submit(),), "investigation", "draft_reply"),
        ("feature", (), (submit(),), "investigation", "draft_reply"),
        ("question", ("An answer.",), (), "draft_reply", "investigation"),
        ("chore", ("Acknowledged.",), (), "draft_reply", "investigation"),
    ],
)
def test_triage_routes_each_type_down_the_right_branch(
    harness: Harness,
    ticket_type: str,
    worker: tuple[Any, ...],
    principal: tuple[Any, ...],
    expected_key: str,
    absent_key: str,
) -> None:
    graph = harness.graph(triage(ticket_type), *worker, principal=principal)
    _start(graph)
    values = graph.get_state(CONFIG).values
    assert expected_key in values
    assert absent_key not in values


def test_checkpoints_cannot_construct_arbitrary_classes(harness: Harness) -> None:
    # State holds only JSON and messages, so a tampered database gets nothing else built.
    tampered = JsonPlusSerializer().dumps_typed(ApprovalDecision(action="approve", actor="x"))
    loaded = harness.checkpointer.serde.loads_typed(tampered)
    assert not isinstance(loaded, ApprovalDecision)


def test_triage_uses_the_cheap_model_and_investigate_the_principal(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    assert len(harness.deps.llms.worker.received) == 1  # type: ignore[attr-defined]
    assert len(harness.deps.llms.principal.received) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("branch", [_bug_graph, _question_graph])
def test_interrupt_blocks_write_back_on_every_branch(harness: Harness, branch: Any) -> None:
    graph = branch(harness)
    result = _start(graph)

    assert harness.writer.calls == []
    assert "__interrupt__" in result
    assert graph.get_state(CONFIG).next == (GATE_NODE,)


def test_write_back_stays_blocked_however_often_the_state_is_read(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    for _ in range(3):
        graph.get_state(CONFIG)
    assert harness.writer.calls == []


def test_every_path_to_the_writer_goes_through_the_gate(harness: Harness) -> None:
    # Guards the topology: a future edge that bypasses the gate fails this test.
    drawable = _bug_graph(harness).get_graph()
    edges = [(e.source, e.target) for e in drawable.edges]
    assert WRITE_NODE in {target for _, target in edges}

    reachable = {"__start__"}
    frontier = ["__start__"]
    while frontier:
        node = frontier.pop()
        for source, target in edges:
            if source == node and target != GATE_NODE and target not in reachable:
                reachable.add(target)
                frontier.append(target)
    assert WRITE_NODE not in reachable


def test_reviewer_sees_triage_plan_and_diff_in_the_interrupt(harness: Harness) -> None:
    graph = _bug_graph(harness, diff=GOOD_PATCH, plan=["Use plus."])
    _start(graph)
    request = _interrupt_value(graph)
    assert request["ticket"]["title"] == "Totals are wrong"
    assert request["triage"]["type"] == "bug"
    assert request["investigation"]["plan"] == ["Use plus."]
    assert request["investigation"]["diff"] == GOOD_PATCH
    assert request["investigation"]["patch_applies"] is True
    assert request["proposal"]["labels"] == ["type:bug", "severity:high", "component:billing"]
    assert "Use plus." in request["proposal"]["comment"]
    assert GOOD_PATCH.strip() in request["proposal"]["comment"]


def test_question_reviewer_sees_the_drafted_reply(harness: Harness) -> None:
    graph = _question_graph(harness)
    _start(graph)
    request = _interrupt_value(graph)
    assert request["draft_reply"] == "Set DEFAULT_CURRENCY."
    assert "Set DEFAULT_CURRENCY." in request["proposal"]["comment"]


def test_stopped_investigation_still_reaches_the_gate_and_says_so(harness: Harness) -> None:
    graph = harness.graph(
        triage("bug"),
        principal=tuple(submit(plan="bad") for _ in range(1)),
    )
    _start(graph)
    request = _interrupt_value(graph)
    assert request["investigation"]["complete"] is False
    assert "incomplete" in request["proposal"]["comment"].lower()


def test_approve_writes_exactly_what_the_reviewer_saw(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    proposal = _interrupt_value(graph)["proposal"]

    decision = ApprovalDecision(action="approve", actor="alice")
    result = _resume(graph, decision)

    assert len(harness.writer.calls) == 1
    call = harness.writer.calls[0]
    assert call.ref == make_ticket().ref
    assert call.comment == proposal["comment"]
    assert call.labels == proposal["labels"]
    assert call.idempotency_key == decision.decision_id
    assert result["outcome"] == "written_back"
    assert graph.get_state(CONFIG).next == ()


def test_edit_writes_the_human_version(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    _resume(
        graph,
        ApprovalDecision(
            action="edit", actor="alice", edited_comment="Better words", edited_labels=["type:bug"]
        ),
    )
    call = harness.writer.calls[0]
    assert call.comment == "Better words"
    assert call.labels == ["type:bug"]


def test_editing_only_labels_keeps_the_proposed_comment(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    proposal = _interrupt_value(graph)["proposal"]
    _resume(graph, ApprovalDecision(action="edit", actor="alice", edited_labels=[]))
    call = harness.writer.calls[0]
    assert call.comment == proposal["comment"]
    assert call.labels == []


def test_reject_never_calls_the_writer_and_records_the_reason(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    result = _resume(
        graph, ApprovalDecision(action="reject", actor="bob", reason="Wrong root cause")
    )

    assert harness.writer.calls == []
    assert result["outcome"] == "rejected"
    assert result["decision"]["reason"] == "Wrong root cause"
    assert graph.get_state(CONFIG).next == ()
    event = next(e for e in harness.deps.store.list_events() if e.action == "decision.reject")
    assert event.actor == "bob"
    assert event.detail["reason"] == "Wrong root cause"


def test_invalid_resume_value_asks_again_and_a_valid_one_then_wins(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)

    graph.invoke(Command(resume={"action": "approve", "actor": ""}), CONFIG, durability=DURABILITY)

    assert harness.writer.calls == []
    assert graph.get_state(CONFIG).next == (GATE_NODE,)
    assert "actor" in _interrupt_value(graph)["invalid_decision"]

    result = _resume(graph, ApprovalDecision(action="reject", actor="bob", reason="No"))
    assert result["outcome"] == "rejected"
    assert harness.writer.calls == []


def test_audit_failure_on_approve_cannot_write_after_a_later_rejection(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    store = harness.deps.store
    real_record = store.record_event

    def failing_once(**kwargs: Any) -> None:
        monkeypatch.setattr(store, "record_event", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_event", failing_once)
    with pytest.raises(sqlite3.OperationalError):
        _resume(graph, ApprovalDecision(action="approve", actor="alice"))

    # The approval was saved before the audit failed, so the gate must not reopen.
    assert graph.get_state(CONFIG).interrupts == ()
    assert harness.writer.calls == []

    result: dict[str, Any] = graph.invoke(None, CONFIG, durability=DURABILITY)
    assert result["outcome"] == "written_back"
    actions = [e.action for e in harness.deps.store.list_events()]
    assert actions.count("decision.approve") == 1
    assert actions.index("write_back.completed") < actions.index("decision.approve")


def test_audit_trail_records_each_step_with_who_did_it(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    decision = ApprovalDecision(action="approve", actor="alice")
    _resume(graph, decision)

    events = list(reversed(harness.deps.store.list_events()))
    assert [(e.actor, e.action) for e in events] == [
        ("agent:triage", "triage.classified"),
        ("system", "approval.requested"),
        ("alice", "decision.approve"),
        ("system", "write_back.completed"),
    ]
    approval = events[2]
    assert approval.detail["decision_id"] == decision.decision_id
    assert approval.detail["final_labels"] == harness.writer.calls[0].labels
    assert approval.detail["final_comment"] == harness.writer.calls[0].comment


def test_edit_audit_records_both_the_proposal_and_the_edit(harness: Harness) -> None:
    graph = _bug_graph(harness)
    _start(graph)
    proposal = _interrupt_value(graph)["proposal"]
    _resume(graph, ApprovalDecision(action="edit", actor="alice", edited_comment="Mine"))
    event = next(e for e in harness.deps.store.list_events() if e.action == "decision.edit")
    assert event.detail["proposed_comment"] == proposal["comment"]
    assert event.detail["final_comment"] == "Mine"


def test_failed_write_back_resumes_without_asking_the_human_again(harness: Harness) -> None:
    harness.writer.failures.append(httpx.ConnectError("GitHub is down"))
    graph = _bug_graph(harness)
    _start(graph)
    decision = ApprovalDecision(action="approve", actor="alice")

    with pytest.raises(httpx.ConnectError):
        _resume(graph, decision)
    assert harness.writer.calls == []
    assert graph.get_state(CONFIG).next == (WRITE_NODE,)
    assert graph.get_state(CONFIG).interrupts == ()

    result: dict[str, Any] = graph.invoke(None, CONFIG, durability=DURABILITY)

    assert result["outcome"] == "written_back"
    assert harness.writer.calls[0].idempotency_key == decision.decision_id
    decisions = [e for e in harness.deps.store.list_events() if e.action == "decision.approve"]
    assert len(decisions) == 1


def test_transient_write_failure_is_retried_within_the_run(tmp_path: Path) -> None:
    writer = SpyWriter(failures=[httpx.ConnectError("blip")])
    deps = make_deps(
        tmp_path,
        worker=scripted(triage("bug")),
        principal=scripted(submit()),
        writer=writer,
        retry=RetryPolicy(max_attempts=2, initial_interval=0.01, jitter=False),
    )
    with open_checkpointer(tmp_path / "app.db") as checkpointer:
        graph = build_graph(deps, checkpointer)
        _start(graph)
        result = _resume(graph, ApprovalDecision(action="approve", actor="alice"))
    assert result["outcome"] == "written_back"
    assert len(writer.calls) == 1


def test_investigation_continues_from_its_last_step_after_a_failure(harness: Harness) -> None:
    # Each agent step is checkpointed, so a throttled model does not repeat paid work.
    graph = harness.graph(
        triage("bug"),
        principal=(
            tool_call("read_file", "c1", path="src/app.py"),
            ConnectionError("throttled"),
            submit(summary="Second attempt."),
        ),
    )
    with pytest.raises(ConnectionError):
        _start(graph)

    graph.invoke(None, CONFIG, durability=DURABILITY)

    principal = harness.deps.llms.principal
    assert len(principal.received) == 3  # type: ignore[attr-defined]
    request = _interrupt_value(graph)
    assert request["investigation"]["summary"] == "Second attempt."
    assert [c["tool"] for c in request["investigation"]["tool_calls"]] == ["read_file"]


def test_diff_too_large_for_a_github_comment_is_truncated_in_the_comment_only(
    harness: Harness,
) -> None:
    huge = GOOD_PATCH + "+# padding\n" * 10_000
    graph = _bug_graph(harness, diff=huge)
    _start(graph)
    request = _interrupt_value(graph)
    assert len(request["proposal"]["comment"]) < 60_000
    assert "truncated" in request["proposal"]["comment"]
    assert request["investigation"]["diff"] == huge


def test_long_text_cannot_cut_through_the_closing_fence(harness: Harness) -> None:
    graph = _bug_graph(harness, summary="s" * 70_000, diff=GOOD_PATCH)
    _start(graph)
    comment = _interrupt_value(graph)["proposal"]["comment"]
    assert len(comment) <= 60_000
    assert "truncated" in comment
    assert comment.count("```") == 2


def test_a_long_backtick_run_cannot_push_the_comment_over_the_limit(harness: Harness) -> None:
    # The fence must outgrow any backtick run, so a hostile diff could otherwise double in size.
    graph = _bug_graph(harness, diff="+" + "`" * 30_000 + "\n")
    _start(graph)
    comment = _interrupt_value(graph)["proposal"]["comment"]
    assert len(comment) <= 60_000
    assert "too large" in comment


def test_a_diff_containing_backticks_cannot_break_out_of_its_fence(harness: Harness) -> None:
    diff = GOOD_PATCH + "+```\n+@everyone look\n"
    graph = _bug_graph(harness, diff=diff)
    _start(graph)
    comment = _interrupt_value(graph)["proposal"]["comment"]
    assert "````diff" in comment
