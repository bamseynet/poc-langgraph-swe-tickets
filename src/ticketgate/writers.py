"""Writers that are not tied to a ticket system."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path

from ticketgate.models import TicketRef, WriteBackResult
from ticketgate.ports import TicketWriter


class LocalFileWriter:
    """Records write-backs in a JSONL file, so the demo can run without touching GitHub."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def write_back(
        self, ref: TicketRef, *, comment: str, labels: list[str], idempotency_key: str
    ) -> WriteBackResult:
        with self._lock:
            if self._already_written(idempotency_key):
                return WriteBackResult(labels=labels, deduplicated=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "thread_id": ref.thread_id,
                "comment": comment,
                "labels": labels,
                "idempotency_key": idempotency_key,
            }
            with self._path.open("a") as file:
                file.write(json.dumps(record) + "\n")
        return WriteBackResult(labels=labels)

    def _already_written(self, idempotency_key: str) -> bool:
        if not self._path.exists():
            return False
        for line in self._path.read_text().splitlines():
            try:
                if json.loads(line)["idempotency_key"] == idempotency_key:
                    return True
            except (ValueError, KeyError, TypeError):
                # A crash can leave a half-written last line. It must not block every later write.
                continue
        return False


class DispatchingWriter:
    """Sends each write-back to the writer for the ticket's source system."""

    def __init__(self, writers: Mapping[str, TicketWriter]) -> None:
        self._writers = writers

    def write_back(
        self, ref: TicketRef, *, comment: str, labels: list[str], idempotency_key: str
    ) -> WriteBackResult:
        writer = self._writers.get(ref.source)
        if writer is None:
            raise LookupError(f"No writer is configured for {ref.source} tickets")
        return writer.write_back(
            ref, comment=comment, labels=labels, idempotency_key=idempotency_key
        )
