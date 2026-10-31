"""The two interfaces between the graph and ticket systems."""

from __future__ import annotations

from typing import Protocol

from ticketgate.models import Ticket, TicketRef, WriteBackResult


class TicketSource(Protocol):
    """Where tickets come from."""

    def list_tickets(self) -> list[Ticket]:
        """Return tickets currently open for processing."""
        ...


class TicketWriter(Protocol):
    """The only way the graph can change anything outside itself.

    Deliberately narrow: a comment and labels. There is no method that could push code.
    """

    def write_back(
        self, ref: TicketRef, *, comment: str, labels: list[str], idempotency_key: str
    ) -> WriteBackResult:
        """Apply an approved write-back.

        Must be safe to call again with the same key. The graph retries this after a crash,
        which may have happened after the comment was posted.
        """
        ...
