"""HTTP API. Blocking graph work runs on worker threads, and progress streams over SSE."""

import asyncio
import contextlib
import hmac
import json
import logging
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from ticketgate.bootstrap import App, open_app
from ticketgate.config import Settings
from ticketgate.models import ApprovalDecision
from ticketgate.service import (
    DiscoverResult,
    ProgressEvent,
    TicketConflict,
    TicketNotFound,
    TicketStatus,
    TicketView,
)
from ticketgate.store import AuditEvent

logger = logging.getLogger(__name__)

MAX_QUEUED_EVENTS = 200


class EventBus:
    """Carries progress from worker threads to the event loop serving SSE clients."""

    def __init__(self, max_queued: int = MAX_QUEUED_EVENTS) -> None:
        self._max_queued = max_queued
        self._lock = threading.Lock()
        self._subscribers: dict[asyncio.Queue[ProgressEvent], asyncio.AbstractEventLoop] = {}

    def subscribe(self) -> asyncio.Queue[ProgressEvent]:
        queue: asyncio.Queue[ProgressEvent] = asyncio.Queue(self._max_queued)
        with self._lock:
            self._subscribers[queue] = asyncio.get_running_loop()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ProgressEvent]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)

    def publish(self, event: ProgressEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            # A closed loop means the client has gone, which is not this publisher's problem.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._deliver, queue, event)

    @staticmethod
    def _deliver(queue: asyncio.Queue[ProgressEvent], event: ProgressEvent) -> None:
        # A slow client loses its backlog, but is told so: it must reload rather than trust a
        # list that may be missing a "finished" or "awaiting_approval" event.
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(ProgressEvent(thread_id="", kind="resync"))


async def event_stream(
    bus: EventBus, queue: asyncio.Queue[ProgressEvent] | None = None
) -> AsyncGenerator[dict[str, str]]:
    # The endpoint subscribes before the response starts, so an event published between the
    # client getting its headers and the first read is not lost.
    queue = queue or bus.subscribe()
    try:
        while True:
            event = await queue.get()
            yield {"data": json.dumps(asdict(event), default=str)}
    finally:
        bus.unsubscribe(queue)


class DecisionRequest(BaseModel):
    # Unknown fields are refused so a client cannot try to set decided_at or decision_id.
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit", "reject"]
    actor: str | None = None
    reason: str | None = None
    edited_comment: str | None = None
    edited_labels: list[str] | None = None


class SessionInfo(BaseModel):
    auth_required: bool
    actor: str | None
    llm_mode: str
    ticket_source: str


class IngestResponse(BaseModel):
    new: list[str]
    errors: list[str]


def _parse_tokens(settings: Settings) -> dict[str, str] | None:
    """Return reviewer name to token, or None when auth is off."""
    if settings.reviewer_tokens is None:
        return None
    try:
        tokens = json.loads(settings.reviewer_tokens.get_secret_value())
    except json.JSONDecodeError as exc:
        raise ValueError("REVIEWER_TOKENS must be a JSON object of name to token") from exc
    valid = (
        isinstance(tokens, dict)
        and tokens
        and all(isinstance(t, str) and t for t in tokens.values())
    )
    if not valid:
        raise ValueError("REVIEWER_TOKENS must be a non-empty JSON object of name to token")
    if len(set(tokens.values())) != len(tokens):
        # Two names on one token would attribute decisions to whichever is listed last.
        raise ValueError("REVIEWER_TOKENS must give each reviewer a different token")
    return dict(tokens)


def _reviewer_for(tokens: dict[str, str], authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    supplied = authorization.removeprefix("Bearer ").encode()
    reviewer = None
    # Check every token, so the response time does not reveal which one nearly matched.
    for name, token in tokens.items():
        if hmac.compare_digest(supplied, token.encode()):
            reviewer = name
    return reviewer


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    tokens = _parse_tokens(resolved)
    bus = EventBus()

    def service_app(request: Request) -> App:
        app_context: App = request.app.state.context
        return app_context

    def require_reviewer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str | None:
        """Return the authenticated reviewer, or None in local development without auth."""
        if tokens is None:
            return None
        reviewer = _reviewer_for(tokens, authorization)
        if reviewer is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid reviewer token is required")
        return reviewer

    Reviewer = Annotated[str | None, Depends(require_reviewer)]
    Context = Annotated[App, Depends(service_app)]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with (
            open_app(resolved, on_progress=bus.publish) as context,
            ThreadPoolExecutor(resolved.worker_threads, thread_name_prefix="ticket") as executor,
        ):
            app.state.context = context
            app.state.executor = executor
            # One task per ticket, so that shutting down can cancel the ones not yet started.
            for thread_id in await run_in_threadpool(context.service.unfinished):
                _submit(executor, context.service.process, thread_id)
            poller = None
            if resolved.poll_interval_seconds > 0:
                poller = asyncio.create_task(_poll(context, executor))
            try:
                yield
            finally:
                if poller:
                    poller.cancel()
                    # Wait, so a poll in flight cannot submit to the executor once it is shut down.
                    await asyncio.gather(poller, return_exceptions=True)
                await run_in_threadpool(executor.shutdown, wait=True, cancel_futures=True)

    async def _poll(context: App, executor: ThreadPoolExecutor) -> None:
        while True:
            await asyncio.sleep(resolved.poll_interval_seconds)
            # Polling is how tickets arrive, so one bad poll must not end it unnoticed.
            try:
                await run_in_threadpool(_discover_and_process, context, executor)
            except Exception:
                logger.exception("Polling for tickets failed")

    # The interactive docs have no login of their own, so with auth on they are switched off.
    docs_enabled = tokens is None
    app = FastAPI(
        title="Ticketgate",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    @app.exception_handler(TicketNotFound)
    def _not_found(_request: Request, exc: TicketNotFound) -> JSONResponse:
        return JSONResponse({"detail": f"No such ticket: {exc}"}, status.HTTP_404_NOT_FOUND)

    @app.exception_handler(TicketConflict)
    def _conflict(_request: Request, exc: TicketConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status.HTTP_409_CONFLICT)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session(authorization: Annotated[str | None, Header()] = None) -> SessionInfo:
        return SessionInfo(
            auth_required=tokens is not None,
            actor=_reviewer_for(tokens, authorization) if tokens else None,
            llm_mode=resolved.llm_mode,
            ticket_source=resolved.ticket_source,
        )

    @app.post("/api/ingest", status_code=status.HTTP_202_ACCEPTED)
    def ingest(request: Request, context: Context, _reviewer: Reviewer) -> IngestResponse:
        result = _discover_and_process(context, request.app.state.executor)
        return IngestResponse(new=result.new, errors=result.errors)

    @app.get("/api/tickets")
    def list_tickets(
        context: Context, _reviewer: Reviewer, status: TicketStatus | None = None
    ) -> list[TicketView]:
        return context.service.list_tickets(status)

    @app.post("/api/tickets/{thread_id:path}/decision")
    def decide(
        thread_id: str, body: DecisionRequest, context: Context, reviewer: Reviewer
    ) -> TicketView:
        # A token decides who is acting. Only local development trusts the request body.
        actor = reviewer if reviewer is not None else body.actor
        try:
            decision = ApprovalDecision.model_validate(
                body.model_dump(exclude={"actor"}) | {"actor": actor or ""}
            )
        except ValidationError as exc:
            detail = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from exc
        return context.service.decide(thread_id, decision)

    @app.post("/api/tickets/{thread_id:path}/retry")
    def retry(thread_id: str, context: Context, _reviewer: Reviewer) -> TicketView:
        return context.service.retry(thread_id)

    @app.get("/api/tickets/{thread_id:path}")
    def get_ticket(thread_id: str, context: Context, _reviewer: Reviewer) -> TicketView:
        return context.service.get(thread_id)

    @app.get("/api/audit")
    def audit(
        context: Context,
        _reviewer: Reviewer,
        thread_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> list[AuditEvent]:
        return context.store.list_events(thread_id, limit)

    @app.get("/api/events")
    async def events(_reviewer: Reviewer) -> EventSourceResponse:
        queue = bus.subscribe()
        # The generator's own cleanup never runs if the client leaves before it starts, so the
        # response also unsubscribes once it ends. Unsubscribing twice is harmless.
        return EventSourceResponse(
            event_stream(bus, queue), background=BackgroundTask(bus.unsubscribe, queue)
        )

    # Mounted last so that it can never shadow an API route.
    if (resolved.web_dist_path / "index.html").exists():
        app.mount("/", StaticFiles(directory=resolved.web_dist_path, html=True), name="web")

    return app


def _discover_and_process(context: App, executor: ThreadPoolExecutor) -> DiscoverResult:
    result = context.service.discover()
    for thread_id in result.new:
        _submit(executor, context.service.process, thread_id)
    return result


def _submit(executor: ThreadPoolExecutor, task: Callable[..., Any], *args: Any) -> None:
    def log_failure(future: Future[Any]) -> None:
        if not future.cancelled() and (exc := future.exception()):
            logger.error("Background task failed", exc_info=exc)

    try:
        future = executor.submit(task, *args)
    except RuntimeError:
        # Shutting down. The ticket is registered, so the next start recovers it.
        logger.warning("Not starting %s: the executor is shut down", args)
        return
    future.add_done_callback(log_failure)
