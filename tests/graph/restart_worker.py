"""Run by test_restart.py in its own process: reach the gate, then die without cleanup."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tests.support import make_deps, make_ticket, scripted, submit, tool_call, triage
from ticketgate.graph import DURABILITY, build_graph, open_checkpointer
from ticketgate.models import WriteBackResult


class ExplodingWriter:
    """Proves the first process never writes."""

    def write_back(self, *args: object, **kwargs: object) -> WriteBackResult:
        raise AssertionError("write_back called before approval")


def main(workdir: Path, thread_id: str, mode: str) -> None:
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


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
