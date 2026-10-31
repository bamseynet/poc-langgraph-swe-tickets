"""Application tables: the append-only audit log and the ticket registry.

These live beside the LangGraph checkpoints, not inside them. Checkpoints are the graph's
working memory and can be replayed or pruned, while an audit log must outlive all of that
and be queryable across tickets.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_thread ON audit_log (thread_id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE IF NOT EXISTS tickets (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL UNIQUE,
    ticket TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
"""


class AuditEvent(BaseModel):
    id: int
    event_key: str
    at: datetime
    actor: str
    action: str
    thread_id: str
    detail: dict[str, Any]


class Store:
    def __init__(self, path: Path | str, *, journal_mode: str = "WAL") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(f"PRAGMA journal_mode={journal_mode}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_event(
        self,
        *,
        event_key: str,
        thread_id: str,
        actor: str,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        """Append an event. A repeated `event_key` is ignored, because nodes can re-run."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO audit_log"
                " (event_key, at, actor, action, thread_id, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_key,
                    datetime.now(UTC).isoformat(),
                    actor,
                    action,
                    thread_id,
                    json.dumps(detail),
                ),
            )

    def list_events(self, thread_id: str | None = None, limit: int = 500) -> list[AuditEvent]:
        """Return events newest first, which is the order the audit view shows."""
        query = "SELECT id, event_key, at, actor, action, thread_id, detail FROM audit_log"
        params: tuple[Any, ...] = (limit,)
        if thread_id is not None:
            query += " WHERE thread_id = ?"
            params = (thread_id, limit)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            AuditEvent(
                id=row[0],
                event_key=row[1],
                at=row[2],
                actor=row[3],
                action=row[4],
                thread_id=row[5],
                detail=json.loads(row[6]),
            )
            for row in rows
        ]

    def register_ticket(self, thread_id: str, ticket: dict[str, Any]) -> bool:
        """Return True the first time a ticket is seen, which makes ingestion idempotent."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO tickets (thread_id, ticket, ingested_at) VALUES (?, ?, ?)",
                (thread_id, json.dumps(ticket), datetime.now(UTC).isoformat()),
            )
            return cursor.rowcount == 1

    def has_ticket(self, thread_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tickets WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None

    def get_ticket(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ticket FROM tickets WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            return None
        ticket: dict[str, Any] = json.loads(row[0])
        return ticket

    def list_thread_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT thread_id FROM tickets ORDER BY seq").fetchall()
        return [row[0] for row in rows]
