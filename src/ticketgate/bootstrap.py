"""The composition root: turns settings into a running application."""

from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

from ticketgate.config import Settings
from ticketgate.github import GitHubIssuesSource, GitHubIssuesWriter, github_client
from ticketgate.graph import GraphDeps, build_graph, open_checkpointer
from ticketgate.jira import JiraSource
from ticketgate.llm import build_llms
from ticketgate.ports import TicketSource, TicketWriter
from ticketgate.service import ProgressEvent, TicketService
from ticketgate.sources import DemoTicketSource
from ticketgate.store import Store
from ticketgate.writers import DispatchingWriter, LocalFileWriter

JIRA_TIMEOUT_SECONDS = 30.0


class ConfigError(ValueError):
    """The settings are inconsistent. Raised at startup, before any ticket is touched."""


@dataclass(frozen=True)
class App:
    service: TicketService
    store: Store
    settings: Settings


def _github_token(settings: Settings) -> str | None:
    return settings.github_token.get_secret_value() if settings.github_token else None


def _build_jira_source(settings: Settings) -> JiraSource:
    required = {
        "JIRA_BASE_URL": settings.jira_base_url,
        "JIRA_EMAIL": settings.jira_email,
        "JIRA_API_TOKEN": settings.jira_api_token,
        "JIRA_JQL": settings.jira_jql,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigError(f"JIRA_ENABLED=true also needs {', '.join(missing)}")
    assert settings.jira_base_url and settings.jira_email and settings.jira_jql
    assert settings.jira_api_token
    client = httpx.Client(
        base_url=settings.jira_base_url,
        auth=(settings.jira_email, settings.jira_api_token.get_secret_value()),
        timeout=JIRA_TIMEOUT_SECONDS,
    )
    return JiraSource(client, base_url=settings.jira_base_url, jql=settings.jira_jql)


def build_sources(settings: Settings) -> list[TicketSource]:
    sources: list[TicketSource] = []
    if settings.ticket_source == "demo":
        sources.append(DemoTicketSource(settings.demo_tickets_path))
    else:
        if not settings.github_repo:
            raise ConfigError("TICKET_SOURCE=github needs GITHUB_REPO")
        sources.append(
            GitHubIssuesSource(
                github_client(_github_token(settings)),
                settings.github_repo,
                label_filter=settings.github_label_filter,
            )
        )
    if settings.jira_enabled:
        sources.append(_build_jira_source(settings))
    return sources


def build_writer(settings: Settings) -> TicketWriter:
    # Jira has no writer: approving a Jira ticket raises, and the ticket shows as failed.
    writers: dict[str, TicketWriter] = {
        "demo": LocalFileWriter(settings.data_dir / "write_backs.jsonl")
    }
    if settings.ticket_source == "github":
        token = _github_token(settings)
        if not token:
            raise ConfigError("TICKET_SOURCE=github needs GITHUB_TOKEN to write back")
        writers["github"] = GitHubIssuesWriter(github_client(token))
    return DispatchingWriter(writers)


@contextmanager
def _exclusive_data_dir(settings: Settings) -> Iterator[None]:
    """Keep a second process off the same data. The kernel releases this even after a crash."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "ticketgate.lock").open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ConfigError(f"Another process is already using {settings.data_dir}") from exc
        yield


@contextmanager
def open_app(
    settings: Settings, *, on_progress: Callable[[ProgressEvent], None] | None = None
) -> Iterator[App]:
    if not settings.target_repo_path.is_dir():
        raise ConfigError(f"TARGET_REPO_PATH is not a directory: {settings.target_repo_path}")
    with _exclusive_data_dir(settings):
        db_path = settings.data_dir / "ticketgate.db"
        store = Store(db_path)
        try:
            with open_checkpointer(db_path) as checkpointer:
                deps = GraphDeps(
                    llms=build_llms(settings),
                    writer=build_writer(settings),
                    store=store,
                    target_repo=settings.target_repo_path,
                    max_investigation_steps=settings.max_investigation_steps,
                )
                service = TicketService(
                    graph=build_graph(deps, checkpointer),
                    store=store,
                    sources=build_sources(settings),
                    on_progress=on_progress,
                    max_new_tickets=settings.max_tickets_per_poll,
                )
                yield App(service=service, store=store, settings=settings)
        finally:
            store.close()
