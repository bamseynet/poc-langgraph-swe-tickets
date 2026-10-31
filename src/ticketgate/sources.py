"""The demo ticket source, for trying the system without any external account."""

from __future__ import annotations

import json
from pathlib import Path

from ticketgate.models import Ticket


class DemoTicketSource:
    def __init__(self, path: Path) -> None:
        self._path = path

    def list_tickets(self) -> list[Ticket]:
        return [Ticket.model_validate(raw) for raw in json.loads(self._path.read_text())]
