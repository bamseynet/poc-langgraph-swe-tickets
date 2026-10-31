from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.support import make_ticket
from ticketgate.api import EventBus, create_app, event_stream
from ticketgate.config import Settings
from ticketgate.service import ProgressEvent, TicketService
from ticketgate.store import Store

BUG = "demo:acme/invoicing#1"
QUESTION = "demo:acme/invoicing#2"
ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}
TOKENS = json.dumps({"alice": "alice-token", "bob": "bob-token"})


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path / "data", poll_interval_seconds=0, **overrides
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path))) as test_client:
        yield test_client


def _path(thread_id: str, suffix: str = "") -> str:
    return f"/api/tickets/{quote(thread_id, safe='')}{suffix}"


def _wait_for_queue(client: TestClient, count: int = 4, **kwargs: Any) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        tickets: list[dict[str, Any]] = client.get(
            "/api/tickets", params={"status": "awaiting_approval"}, **kwargs
        ).json()
        if len(tickets) >= count:
            return tickets
        time.sleep(0.05)
    raise AssertionError("Tickets never reached the approval queue")


def _ingest(client: TestClient, **kwargs: Any) -> None:
    response = client.post("/api/ingest", **kwargs)
    assert response.status_code == 202
    _wait_for_queue(client, **kwargs)


def test_health_needs_no_auth(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, reviewer_tokens=SecretStr(TOKENS)))) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_session_reports_the_mode_without_auth(client: TestClient) -> None:
    assert client.get("/api/session").json() == {
        "auth_required": False,
        "actor": None,
        "llm_mode": "fake",
        "ticket_source": "demo",
    }


def test_ingest_returns_new_tickets_and_processes_them_in_the_background(
    client: TestClient,
) -> None:
    response = client.post("/api/ingest")
    assert response.status_code == 202
    assert len(response.json()["new"]) == 4
    assert len(_wait_for_queue(client)) == 4


def test_second_ingest_finds_nothing_new(client: TestClient) -> None:
    _ingest(client)
    assert client.post("/api/ingest").json()["new"] == []


def test_a_source_failure_is_reported_rather_than_looking_like_no_tickets(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.json"
    with TestClient(create_app(_settings(tmp_path, demo_tickets_path=missing))) as client:
        body = client.post("/api/ingest").json()
    assert body["new"] == []
    assert "DemoTicketSource" in body["errors"][0]


def test_ticket_detail_is_reachable_despite_slash_and_hash_in_its_id(client: TestClient) -> None:
    _ingest(client)
    body = client.get(_path(BUG)).json()
    assert body["thread_id"] == BUG
    assert body["status"] == "awaiting_approval"
    assert body["triage"]["type"] == "bug"
    assert body["investigation"]["diff"]
    assert body["proposal"]["comment"]


def test_unknown_ticket_is_404(client: TestClient) -> None:
    assert client.get(_path("nope")).status_code == 404
    assert (
        client.post(
            _path("nope", "/decision"), json={"action": "approve", "actor": "a"}
        ).status_code
        == 404
    )
    assert client.post(_path("nope", "/retry"), json={}).status_code == 404


def test_status_filter_rejects_an_unknown_status(client: TestClient) -> None:
    assert client.get("/api/tickets", params={"status": "nope"}).status_code == 422


def test_approve_writes_back_and_records_who(client: TestClient, tmp_path: Path) -> None:
    _ingest(client)
    log = tmp_path / "data" / "write_backs.jsonl"
    assert not log.exists()

    response = client.post(_path(BUG, "/decision"), json={"action": "approve", "actor": "alice"})

    assert response.status_code == 200
    assert response.json()["status"] == "written_back"
    assert log.exists()
    event = next(e for e in client.get("/api/audit").json() if e["action"] == "decision.approve")
    assert event["actor"] == "alice"
    assert event["thread_id"] == BUG


def test_reject_requires_a_reason_and_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    _ingest(client)
    refused = client.post(_path(BUG, "/decision"), json={"action": "reject", "actor": "bob"})
    assert refused.status_code == 422
    assert "reason" in refused.json()["detail"][0]["msg"]
    assert client.get(_path(BUG)).json()["status"] == "awaiting_approval"

    done = client.post(
        _path(BUG, "/decision"), json={"action": "reject", "actor": "bob", "reason": "Wrong"}
    )
    assert done.json()["status"] == "rejected"
    assert not (tmp_path / "data" / "write_backs.jsonl").exists()


def test_edit_writes_the_edited_version(client: TestClient, tmp_path: Path) -> None:
    _ingest(client)
    client.post(
        _path(QUESTION, "/decision"),
        json={"action": "edit", "actor": "alice", "edited_comment": "Mine", "edited_labels": []},
    )
    record = json.loads((tmp_path / "data" / "write_backs.jsonl").read_text())
    assert record["comment"] == "Mine"
    assert record["labels"] == []


def test_second_decision_is_a_409(client: TestClient) -> None:
    _ingest(client)
    body = {"action": "approve", "actor": "alice"}
    assert client.post(_path(BUG, "/decision"), json=body).status_code == 200
    assert client.post(_path(BUG, "/decision"), json=body).status_code == 409


def test_decision_without_an_actor_is_refused_in_dev_mode(client: TestClient) -> None:
    _ingest(client)
    assert client.post(_path(BUG, "/decision"), json={"action": "approve"}).status_code == 422


def test_unknown_decision_fields_are_refused(client: TestClient) -> None:
    _ingest(client)
    response = client.post(
        _path(BUG, "/decision"),
        json={"action": "approve", "actor": "a", "decided_at": "2000-01-01T00:00:00Z"},
    )
    assert response.status_code == 422


def test_retry_refuses_a_ticket_waiting_for_a_human(client: TestClient, tmp_path: Path) -> None:
    _ingest(client)
    assert client.post(_path(BUG, "/retry"), json={}).status_code == 409
    assert not (tmp_path / "data" / "write_backs.jsonl").exists()


def test_audit_filters_by_ticket(client: TestClient) -> None:
    _ingest(client)
    events = client.get("/api/audit", params={"thread_id": BUG}).json()
    assert events
    assert {e["thread_id"] for e in events} == {BUG}


def test_pending_approvals_survive_an_api_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as first:
        _ingest(first)

    with TestClient(create_app(settings)) as second:
        assert len(_wait_for_queue(second)) == 4
        response = second.post(_path(BUG, "/decision"), json={"action": "approve", "actor": "a"})
        assert response.json()["status"] == "written_back"


@pytest.fixture
def secured(tmp_path: Path) -> Iterator[TestClient]:
    settings = _settings(tmp_path, reviewer_tokens=SecretStr(TOKENS))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/tickets"),
        ("get", "/api/audit"),
        ("get", "/api/events"),
        ("post", "/api/ingest"),
        ("get", _path(BUG)),
        ("post", _path(BUG, "/decision")),
        ("post", _path(BUG, "/retry")),
    ],
)
def test_every_route_needs_a_token_when_auth_is_on(
    secured: TestClient, method: str, path: str
) -> None:
    assert secured.request(method, path).status_code == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert secured.request(method, path, headers=wrong).status_code == 401


def test_session_names_the_reviewer_from_the_token(secured: TestClient) -> None:
    assert secured.get("/api/session").json()["actor"] is None
    assert secured.get("/api/session").json()["auth_required"] is True
    assert secured.get("/api/session", headers=BOB).json()["actor"] == "bob"


def test_identity_comes_from_the_token_not_the_request_body(secured: TestClient) -> None:
    _ingest(secured, headers=ALICE)
    response = secured.post(
        _path(BUG, "/decision"), headers=ALICE, json={"action": "approve", "actor": "bob"}
    )
    assert response.status_code == 200
    events = secured.get("/api/audit", headers=ALICE).json()
    assert next(e for e in events if e["action"] == "decision.approve")["actor"] == "alice"


@pytest.mark.parametrize("config", ["not json", '{"alice": ""}', "{}"])
def test_malformed_token_config_fails_at_startup(tmp_path: Path, config: str) -> None:
    settings = _settings(tmp_path, reviewer_tokens=SecretStr(config))
    with pytest.raises(ValueError, match="REVIEWER_TOKENS"), TestClient(create_app(settings)):
        pass


def test_serves_the_built_ui_when_it_exists(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>built ui</title>")
    with TestClient(create_app(_settings(tmp_path, web_dist_path=dist))) as client:
        assert "built ui" in client.get("/").text
        assert client.get("/api/health").json() == {"status": "ok"}


def test_bus_delivers_events_published_from_another_thread() -> None:
    async def scenario() -> ProgressEvent:
        bus = EventBus()
        queue = bus.subscribe()
        event = ProgressEvent("t", "node", "triage")
        await asyncio.to_thread(bus.publish, event)
        received = await asyncio.wait_for(queue.get(), timeout=2)
        bus.unsubscribe(queue)
        return received

    assert asyncio.run(scenario()).node == "triage"


def test_bus_tells_a_stuck_subscriber_to_resync_instead_of_dropping_silently() -> None:
    async def scenario() -> list[str]:
        bus = EventBus(max_queued=2)
        queue = bus.subscribe()
        for _ in range(5):
            bus.publish(ProgressEvent("t", "node", "x"))
        await asyncio.sleep(0)
        kinds: list[str] = []
        while not queue.empty():
            kinds.append(queue.get_nowait().kind)
        return kinds

    kinds = asyncio.run(scenario())
    assert len(kinds) <= 2
    assert kinds[-1] == "resync"


def test_event_stream_serialises_events_as_sse_data() -> None:
    async def scenario() -> dict[str, Any]:
        bus = EventBus()
        stream = event_stream(bus)
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        bus.publish(ProgressEvent("t", "failed", detail={"error": "boom"}))
        item = await asyncio.wait_for(pending, timeout=2)
        await stream.aclose()
        return dict(item)

    item = asyncio.run(scenario())
    assert json.loads(item["data"]) == {
        "thread_id": "t",
        "kind": "failed",
        "node": None,
        "detail": {"error": "boom"},
    }


def test_docs_are_behind_the_login_when_auth_is_on(secured: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert secured.get(path).status_code == 404


def test_a_polling_failure_is_logged_and_polling_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def failing_discover(_self: TicketService) -> Any:
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(TicketService, "discover", failing_discover)
    settings = _settings(tmp_path).model_copy(update={"poll_interval_seconds": 0.05})
    with TestClient(create_app(settings)):
        deadline = time.monotonic() + 10
        while calls < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert calls >= 3
    assert "database is locked" in caplog.text


def test_shutdown_does_not_work_through_every_unfinished_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, worker_threads=1)
    settings.data_dir.mkdir(parents=True)
    store = Store(settings.data_dir / "ticketgate.db")
    for key in range(5):
        store.register_ticket(f"t{key}", make_ticket(str(key)).model_dump(mode="json"))
    store.close()

    processed: list[str] = []

    def slow_process(_self: TicketService, thread_id: str) -> None:
        processed.append(thread_id)
        time.sleep(0.2)

    monkeypatch.setattr(TicketService, "process", slow_process)
    with TestClient(create_app(settings)):
        deadline = time.monotonic() + 10
        while not processed and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(processed) < 5
