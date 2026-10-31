"""Run by test_restart.py in its own process: reach the gate, then die without cleanup."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.support import SpyWriter, make_deps, make_ticket, scripted, submit, tool_call, triage
from ticketgate.graph import DURABILITY, build_graph, open_checkpointer
from ticketgate.models import ApprovalDecision, Ticket, WriteBackResult
from ticketgate.service import TicketService


class ExplodingWriter:
    """Proves the first process never writes."""

    def write_back(self, *args: object, **kwargs: object) -> WriteBackResult:
        raise AssertionError("write_back called before approval")


def _die_after_write_to(checkpointer: Any, channel: str) -> None:
    """Exit straight after a node's result is saved and before the step is checkpointed."""
    real_put_writes = checkpointer.put_writes

    def put_writes(config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        real_put_writes(config, writes, task_id, task_path)
        if any(written == channel for written, _ in writes):
            os._exit(44)

    checkpointer.put_writes = put_writes


def main(workdir: Path, thread_id: str, mode: str) -> None:
    if mode.startswith("service:"):
        _service_run(workdir, thread_id, mode.removeprefix("service:"))
        return
    if mode == "die-mid-investigation":
        principal = scripted(tool_call("read_file", "c1", path="src/app.py"), lambda: os._exit(43))
    else:
        principal = scripted(submit(summary="From the first process."))
    deps = make_deps(
        workdir,
        worker=scripted(triage("bug")),
        principal=principal,
        writer=ExplodingWriter(),
    )
    with open_checkpointer(workdir / "app.db") as checkpointer:
        graph = build_graph(deps, checkpointer)
        graph.invoke(
            {"ticket": make_ticket().model_dump(mode="json")},
            {"configurable": {"thread_id": thread_id}},
            durability=DURABILITY,
        )
        # A hard exit: no context manager cleanup, no atexit, as in a crash or a SIGKILL.
        os._exit(42)


def _service_run(workdir: Path, thread_id: str, channel: str) -> None:
    deps = make_deps(
        workdir,
        worker=scripted(triage("bug")),
        principal=scripted(submit(summary="First process.")),
        writer=SpyWriter(),
    )
    ticket = make_ticket()
    with open_checkpointer(workdir / "app.db") as checkpointer:
        graph = build_graph(deps, checkpointer)
        service = TicketService(graph=graph, store=deps.store, sources=[StaticSource(ticket)])
        if channel in ("triage", "proposal"):
            _die_after_write_to(checkpointer, channel)
        service.ingest()
        if channel in ("decision", "__resume__"):
            _die_after_write_to(checkpointer, channel)
            service.decide(thread_id, ApprovalDecision(action="approve", actor="alice"))
        os._exit(42)


@dataclass
class StaticSource:
    ticket: Ticket

    def list_tickets(self) -> list[Ticket]:
        return [self.ticket]


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
