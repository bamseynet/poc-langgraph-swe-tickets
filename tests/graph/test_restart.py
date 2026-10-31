"""A pending approval survives the process that created it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from tests.support import SpyWriter, make_deps, scripted, submit
from ticketgate.graph import DURABILITY, GATE_NODE, build_graph, open_checkpointer
from ticketgate.models import ApprovalDecision

THREAD_ID = "github:octo-org/octo-repo#101"
CONFIG: Any = {"configurable": {"thread_id": THREAD_ID}}
REPO_ROOT = Path(__file__).parents[2]


def _run_worker(workdir: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.graph.restart_worker", str(workdir), THREAD_ID, mode],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _first_process_reaches_the_gate_and_dies(workdir: Path) -> None:
    result = _run_worker(workdir, "die-at-gate")
    assert result.returncode == 42, result.stderr


def test_approval_resumes_in_a_new_process(tmp_path: Path) -> None:
    _first_process_reaches_the_gate_and_dies(tmp_path)

    # Empty scripts: if anything re-ran triage or the investigator, the model would raise.
    writer = SpyWriter()
    deps = make_deps(tmp_path, worker=scripted(), principal=scripted(), writer=writer)
    with open_checkpointer(tmp_path / "app.db") as checkpointer:
        graph = build_graph(deps, checkpointer)

        snapshot = graph.get_state(CONFIG)
        assert snapshot.next == (GATE_NODE,)
        request = snapshot.interrupts[0].value
        assert request["investigation"]["summary"] == "From the first process."
        assert writer.calls == []

        decision = ApprovalDecision(action="approve", actor="alice")
        result = graph.invoke(
            Command(resume=decision.model_dump(mode="json")), CONFIG, durability=DURABILITY
        )

    assert result["outcome"] == "written_back"
    assert len(writer.calls) == 1
    assert "From the first process." in writer.calls[0].comment


def test_rejection_resumes_in_a_new_process(tmp_path: Path) -> None:
    _first_process_reaches_the_gate_and_dies(tmp_path)

    writer = SpyWriter()
    deps = make_deps(tmp_path, worker=scripted(), principal=scripted(), writer=writer)
    with open_checkpointer(tmp_path / "app.db") as checkpointer:
        graph = build_graph(deps, checkpointer)
        decision = ApprovalDecision(action="reject", actor="bob", reason="No")
        result = graph.invoke(
            Command(resume=decision.model_dump(mode="json")), CONFIG, durability=DURABILITY
        )

    assert result["outcome"] == "rejected"
    assert writer.calls == []


def test_a_crash_mid_investigation_resumes_without_repeating_finished_steps(
    tmp_path: Path,
) -> None:
    # The first process dies inside its second model call, with no chance to clean up.
    result = _run_worker(tmp_path, "die-mid-investigation")
    assert result.returncode == 43, result.stderr

    worker = scripted()  # Would raise if triage ran again.
    principal = scripted(submit(summary="Finished by the second process."))
    deps = make_deps(tmp_path, worker=worker, principal=principal, writer=SpyWriter())
    with open_checkpointer(tmp_path / "app.db") as checkpointer:
        graph = build_graph(deps, checkpointer)
        graph.invoke(None, CONFIG, durability=DURABILITY)
        snapshot = graph.get_state(CONFIG)

    assert snapshot.next == (GATE_NODE,)
    request = snapshot.interrupts[0].value
    assert request["investigation"]["summary"] == "Finished by the second process."
    assert [c["tool"] for c in request["investigation"]["tool_calls"]] == ["read_file"]
    # The new process was handed the tool result the old process had already earned.
    assert any(isinstance(m, ToolMessage) for m in principal.received[0])
