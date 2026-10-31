"""Test doubles and shared builders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import RetryPolicy
from pydantic import ConfigDict, Field

from ticketgate.graph import GraphDeps
from ticketgate.llm import Llms
from ticketgate.models import Ticket, TicketRef, WriteBackResult
from ticketgate.store import Store


class ScriptedChatModel(BaseChatModel):
    """Plays back a fixed list of responses, recording what it was sent.

    Copies from bind_tools share the same script, as copies of a real model share a backend.
    An Exception in the script is raised instead of returned, and a callable is called.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[Any]
    received: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[LanguageModelInput, Any]:
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"bound_tools": names})

    def _next(self, messages: Sequence[BaseMessage]) -> Any:
        self.received.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedChatModel ran out of responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[LanguageModelInput, Any]:
        return RunnableLambda(lambda messages: schema.model_validate(self._next(messages)))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        item = self._next(messages)
        message = AIMessage(content=item) if isinstance(item, str) else item
        return ChatResult(generations=[ChatGeneration(message=message)])


def scripted(*script: Any) -> ScriptedChatModel:
    return ScriptedChatModel(script=list(script), received=[])


def tool_call(name: str, call_id: str = "c1", **args: Any) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def submit(**overrides: Any) -> AIMessage:
    args = {"summary": "The sum subtracts.", "plan": ["Use plus."], "diff": None} | overrides
    return tool_call("submit_investigation", "submit", **args)


def triage(ticket_type: str = "bug", severity: str = "high") -> dict[str, str]:
    return {
        "type": ticket_type,
        "severity": severity,
        "component": "billing",
        "rationale": "Scripted.",
    }


def make_ticket(key: str = "101", title: str = "Totals are wrong") -> Ticket:
    return Ticket(
        ref=TicketRef(source="github", project="octo-org/octo-repo", key=key),
        title=title,
        body="The total is off.",
        author="octocat",
        url=f"https://github.com/octo-org/octo-repo/issues/{key}",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


GOOD_PATCH = """\
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def make_target_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    return root


@dataclass
class WriteBackCall:
    ref: TicketRef
    comment: str
    labels: list[str]
    idempotency_key: str


@dataclass
class SpyWriter:
    """Records calls. Raises each queued failure before recording a successful call."""

    calls: list[WriteBackCall] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)

    def write_back(
        self, ref: TicketRef, *, comment: str, labels: list[str], idempotency_key: str
    ) -> WriteBackResult:
        if self.failures:
            raise self.failures.pop(0)
        self.calls.append(WriteBackCall(ref, comment, labels, idempotency_key))
        return WriteBackResult(comment_url="https://example.test/comment/1", labels=labels)


def make_deps(
    tmp_path: Path,
    *,
    worker: BaseChatModel,
    principal: BaseChatModel | None = None,
    writer: Any = None,
    max_investigation_steps: int = 6,
    retry: RetryPolicy | None = None,
) -> GraphDeps:
    target = tmp_path / "target"
    if not target.exists():
        make_target_repo(target)
    return GraphDeps(
        llms=Llms(principal=principal or scripted(), worker=worker),
        writer=writer if writer is not None else SpyWriter(),
        store=Store(tmp_path / "app.db"),
        target_repo=target,
        max_investigation_steps=max_investigation_steps,
        # One attempt by default, so a scripted failure surfaces immediately.
        retry=retry or RetryPolicy(max_attempts=1),
    )
