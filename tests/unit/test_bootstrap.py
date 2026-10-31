"""The composition root: what `make dev` runs, using the real fake LLM and demo data."""

from __future__ import annotations

import errno
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ticketgate.bootstrap import ConfigError, build_sources, build_writer, open_app
from ticketgate.config import Settings
from ticketgate.github import GitHubIssuesSource
from ticketgate.jira import JiraSource
from ticketgate.models import ApprovalDecision, TicketRef
from ticketgate.service import TicketStatus
from ticketgate.sources import DemoTicketSource


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", **overrides)  # type: ignore[call-arg]


def test_defaults_need_no_credentials_and_use_demo_data(tmp_path: Path) -> None:
    sources = build_sources(_settings(tmp_path))
    assert len(sources) == 1
    assert isinstance(sources[0], DemoTicketSource)


def test_jira_is_off_by_default(tmp_path: Path) -> None:
    assert not any(isinstance(s, JiraSource) for s in build_sources(_settings(tmp_path)))


def test_jira_switch_adds_the_adapter(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        jira_enabled=True,
        jira_base_url="https://example.atlassian.net",
        jira_email="a@example.test",
        jira_api_token=SecretStr("t"),
        jira_jql="project = SHOP",
    )
    assert any(isinstance(s, JiraSource) for s in build_sources(settings))


def test_enabling_jira_without_its_settings_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="JIRA_BASE_URL"):
        build_sources(_settings(tmp_path, jira_enabled=True))


def test_github_source_needs_a_repo(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="GITHUB_REPO"):
        build_sources(_settings(tmp_path, ticket_source="github"))


def test_github_source_uses_the_configured_repo(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ticket_source="github", github_repo="octo-org/octo-repo")
    assert isinstance(build_sources(settings)[0], GitHubIssuesSource)


def test_github_writes_need_a_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ticket_source="github", github_repo="octo-org/octo-repo")
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        build_writer(settings)


def test_approving_a_jira_ticket_fails_clearly_because_no_writer_exists(tmp_path: Path) -> None:
    writer = build_writer(_settings(tmp_path))
    ref = TicketRef(source="jira", project="SHOP", key="SHOP-1")
    with pytest.raises(LookupError, match="jira"):
        writer.write_back(ref, comment="c", labels=[], idempotency_key="k")


def test_demo_writer_does_not_repeat_a_write(tmp_path: Path) -> None:
    writer = build_writer(_settings(tmp_path))
    ref = TicketRef(source="demo", project="p", key="1")
    first = writer.write_back(ref, comment="c", labels=["l"], idempotency_key="k")
    second = writer.write_back(ref, comment="c", labels=["l"], idempotency_key="k")
    assert first.deduplicated is False
    assert second.deduplicated is True
    lines = (tmp_path / "data" / "write_backs.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["comment"] == "c"


def test_demo_runs_end_to_end_offline(tmp_path: Path) -> None:
    with open_app(_settings(tmp_path)) as app:
        thread_ids = app.service.ingest()
        assert len(thread_ids) == 4
        views = {v.title: v for v in app.service.list_tickets()}
        assert all(v.status is TicketStatus.AWAITING_APPROVAL for v in views.values())

        bug = views["Invoice total is wrong when a discount is applied"]
        assert bug.triage is not None
        assert bug.triage["type"] == "bug"
        assert bug.investigation is not None
        assert bug.investigation["patch_applies"] is True

        question = views["How do I change the default invoice currency?"]
        assert question.draft_reply is not None
        assert "DEFAULT_CURRENCY" in question.draft_reply

        feature = views["Add CSV export for invoices"]
        assert feature.investigation is not None
        assert feature.investigation["diff"] is None

        assert not (tmp_path / "data" / "write_backs.jsonl").exists()

        done = app.service.decide(bug.thread_id, ApprovalDecision(action="approve", actor="alice"))
        assert done.status is TicketStatus.WRITTEN_BACK
        assert (tmp_path / "data" / "write_backs.jsonl").exists()


def test_state_survives_reopening_the_app(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with open_app(settings) as app:
        app.service.ingest()
    with open_app(settings) as app:
        assert app.service.ingest() == []
        assert len(app.service.list_tickets(TicketStatus.AWAITING_APPROVAL)) == 4


def test_a_second_process_on_the_same_data_cannot_start(tmp_path: Path) -> None:
    # The per-ticket lock is in-process, so two processes could both approve a ticket.
    settings = _settings(tmp_path)
    with open_app(settings), pytest.raises(ConfigError, match="already"), open_app(settings):
        pass


def test_the_data_lock_is_released_on_exit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with open_app(settings):
        pass
    with open_app(settings):
        pass


def test_a_missing_target_checkout_fails_at_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path, target_repo_path=tmp_path / "missing")
    with pytest.raises(ConfigError, match="TARGET_REPO_PATH"), open_app(settings):
        pass


def test_a_locking_error_is_not_reported_as_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_file: Any, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr("ticketgate.bootstrap.fcntl.flock", fail)
    with pytest.raises(OSError, match="No locks available"), open_app(_settings(tmp_path)):
        pass


def test_a_killed_process_releases_the_data_lock(tmp_path: Path) -> None:
    code = (
        "import os, sys; from pathlib import Path\n"
        "from ticketgate.bootstrap import open_app\n"
        "from ticketgate.config import Settings\n"
        "with open_app(Settings(_env_file=None, data_dir=Path(sys.argv[1]))):\n"
        "    os._exit(45)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "data")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 45, result.stderr
    with open_app(_settings(tmp_path)):
        pass


def test_a_torn_write_back_log_line_does_not_block_later_writes(tmp_path: Path) -> None:
    from ticketgate.models import TicketRef
    from ticketgate.writers import LocalFileWriter

    log = tmp_path / "wb.jsonl"
    log.write_text('{"idempotency_key": "a"}\n{"idempotency_ke')
    writer = LocalFileWriter(log)
    ref = TicketRef(source="demo", project="p", key="1")
    assert writer.write_back(ref, comment="c", labels=[], idempotency_key="b").deduplicated is False
    assert writer.write_back(ref, comment="c", labels=[], idempotency_key="a").deduplicated is True


def test_a_torn_last_line_is_not_glued_to_the_next_record(tmp_path: Path) -> None:
    from ticketgate.models import TicketRef
    from ticketgate.writers import LocalFileWriter

    log = tmp_path / "wb.jsonl"
    log.write_text('{"idempotency_key": "a"}\n{"idempotency_ke')
    ref = TicketRef(source="demo", project="p", key="1")
    LocalFileWriter(log).write_back(ref, comment="c", labels=[], idempotency_key="b")
    writer = LocalFileWriter(log)
    assert writer.write_back(ref, comment="c", labels=[], idempotency_key="b").deduplicated


def _journal_mode(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


def test_uses_wal_by_default(tmp_path: Path) -> None:
    with open_app(_settings(tmp_path)):
        assert _journal_mode(tmp_path / "data" / "ticketgate.db") == "wal"


def test_can_avoid_wal_on_a_network_file_system(tmp_path: Path) -> None:
    # SQLite's write-ahead log needs shared memory, which a network file system cannot give it.
    with open_app(_settings(tmp_path, sqlite_journal_mode="DELETE")) as app:
        assert _journal_mode(tmp_path / "data" / "ticketgate.db") == "delete"
        assert app.service.ingest()
        assert not list((tmp_path / "data").glob("*-wal"))
    # Reopening must not leave it in WAL either.
    with open_app(_settings(tmp_path, sqlite_journal_mode="DELETE")):
        assert _journal_mode(tmp_path / "data" / "ticketgate.db") == "delete"


def test_can_wait_for_a_stopped_process_to_release_the_data_lock(tmp_path: Path) -> None:
    # On a network file system a lock can outlast the task that held it for a while.
    settings = _settings(tmp_path, data_lock_wait_seconds=10)
    held = threading.Event()

    def hold_then_release() -> None:
        with open_app(_settings(tmp_path)):
            held.set()
            time.sleep(0.3)

    holder = threading.Thread(target=hold_then_release)
    holder.start()
    held.wait(timeout=10)
    try:
        with open_app(settings) as app:
            assert app.service.list_tickets() == []
    finally:
        holder.join()


def test_gives_up_after_the_wait(tmp_path: Path) -> None:
    settings = _settings(tmp_path, data_lock_wait_seconds=0.2)
    with (
        open_app(_settings(tmp_path)),
        pytest.raises(ConfigError, match="already"),
        open_app(settings),
    ):
        pass


def test_the_placeholder_github_token_is_refused_with_a_clear_error(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        ticket_source="github",
        github_repo="o/r",
        github_token=SecretStr("REPLACE_ME_AFTER_DEPLOY"),
    )
    with pytest.raises(ConfigError, match="placeholder"):
        build_sources(settings)


def test_a_non_wal_database_is_never_switched_to_wal_while_being_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    from ticketgate.graph import open_checkpointer

    real_setup = SqliteSaver.setup

    def forbidden(self: SqliteSaver) -> None:
        if self.is_setup:
            return
        in_memory = self.conn.execute("PRAGMA database_list").fetchone()[2] == ""
        if not in_memory:
            raise AssertionError("setup() switches the database to WAL")
        real_setup(self)

    path = tmp_path / "fresh.db"
    with monkeypatch.context() as patch:
        patch.setattr(SqliteSaver, "setup", forbidden)
        with open_checkpointer(path, journal_mode="DELETE") as saver:
            assert saver.get_tuple({"configurable": {"thread_id": "x"}}) is None
    assert _journal_mode(path) == "delete"
    assert not list(tmp_path.glob("*-wal"))
