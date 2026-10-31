from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ticketgate.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "app.db")


def _record(
    store: Store, key: str = "k1", thread: str = "t1", action: str = "decision.approve"
) -> None:
    store.record_event(
        event_key=key, thread_id=thread, actor="alice", action=action, detail={"why": "ok"}
    )


def test_records_who_when_and_what(store: Store) -> None:
    _record(store)
    event = store.list_events()[0]
    assert event.actor == "alice"
    assert event.action == "decision.approve"
    assert event.thread_id == "t1"
    assert event.detail == {"why": "ok"}
    assert event.at.tzinfo is not None


def test_replaying_a_node_keeps_the_original_event(store: Store) -> None:
    # LangGraph re-runs a node after a crash or a retry, so writes must be idempotent, and a
    # replay must never rewrite what was recorded the first time.
    store.record_event(event_key="same", thread_id="t", actor="alice", action="a", detail={"n": 1})
    original = store.list_events()[0]
    store.record_event(
        event_key="same", thread_id="t", actor="mallory", action="a", detail={"n": 2}
    )
    assert store.list_events() == [original]


def test_lists_newest_first_and_filters_by_thread(store: Store) -> None:
    _record(store, key="a", thread="t1")
    _record(store, key="b", thread="t2")
    _record(store, key="c", thread="t1")
    assert [e.event_key for e in store.list_events()] == ["c", "b", "a"]
    assert [e.event_key for e in store.list_events(thread_id="t1")] == ["c", "a"]


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"],
)
def test_audit_log_is_append_only_at_the_database_level(
    store: Store, tmp_path: Path, statement: str
) -> None:
    _record(store)
    conn = sqlite3.connect(tmp_path / "app.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)
    conn.close()
    assert store.list_events()[0].actor == "alice"


def test_events_survive_reopening_the_database(tmp_path: Path) -> None:
    first = Store(tmp_path / "app.db")
    _record(first)
    first.close()
    assert len(Store(tmp_path / "app.db").list_events()) == 1


def test_register_ticket_reports_only_the_first_sighting(store: Store) -> None:
    assert store.register_ticket("t1", {"title": "x"}) is True
    assert store.register_ticket("t1", {"title": "changed"}) is False
    assert store.get_ticket("t1") == {"title": "x"}


def test_lists_registered_tickets_in_ingest_order(store: Store) -> None:
    store.register_ticket("t2", {"title": "b"})
    store.register_ticket("t1", {"title": "a"})
    assert store.list_thread_ids() == ["t2", "t1"]


def test_unknown_ticket_is_none(store: Store) -> None:
    assert store.get_ticket("nope") is None
