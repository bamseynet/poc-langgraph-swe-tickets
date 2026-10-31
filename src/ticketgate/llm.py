"""Chat models: real Bedrock, or a deterministic fake that needs no AWS account."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from botocore.config import Config
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from ticketgate.config import Settings

# AWS advises at least 120s: the first request with a new schema can spend ~90s compiling it.
BEDROCK_READ_TIMEOUT_SECONDS = 180
WORKER_MAX_TOKENS = 4_096
PRINCIPAL_MAX_TOKENS = 16_000


@dataclass(frozen=True)
class Llms:
    """`principal` does the expensive reading of code. `worker` triages and drafts replies."""

    principal: BaseChatModel
    worker: BaseChatModel


def build_llms(settings: Settings, *, client: Any = None) -> Llms:
    if settings.llm_mode == "fake":
        return Llms(principal=FakeChatModel(), worker=FakeChatModel())

    def bedrock(model_id: str, max_tokens: int) -> ChatBedrockConverse:
        # No temperature: these models run adaptive thinking by default.
        return ChatBedrockConverse(
            model=model_id,
            region_name=settings.aws_region,
            max_tokens=max_tokens,
            client=client,
            config=Config(
                read_timeout=BEDROCK_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "total_max_attempts": 4},
            ),
        )

    return Llms(
        principal=bedrock(settings.bedrock_model_id, PRINCIPAL_MAX_TOKENS),
        worker=bedrock(settings.bedrock_worker_model_id, WORKER_MAX_TOKENS),
    )


_DISCOUNT_FIX = """\
--- a/src/invoicing/discounts.py
+++ b/src/invoicing/discounts.py
@@ -5,4 +5,4 @@ def apply_discount(subtotal_cents: int, percent: int) -> int:
     \"\"\"Return the amount due, in cents, after a percentage discount.\"\"\"
     if percent < 0 or percent > 100:
         raise ValueError("percent must be between 0 and 100")
-    return subtotal_cents - subtotal_cents // (100 // percent)
+    return subtotal_cents - subtotal_cents * percent // 100
"""

_CHORE_WORDS = ("bump", "upgrade", "dependency", "dependencies", "cleanup", "clean up", "rename")
_FEATURE_WORDS = ("add ", "support ", "feature", "would like", "export")
_QUESTION_STARTS = ("how ", "where ", "what ", "why ", "when ", "can i ", "is there ")
_HIGH_SEVERITY_WORDS = ("crash", "wrong", "data loss", "outage", "error")
_COMPONENTS = {
    "discount": "billing",
    "invoice": "billing",
    "currency": "settings",
    "export": "reporting",
    "linter": "build",
}
_SEARCH_TERMS = ("discount", "export", "invoice", "currency")


def _classify(text: str) -> dict[str, str]:
    lowered = text.lower().strip()
    ticket_type = "bug"
    if "?" in lowered or lowered.startswith(_QUESTION_STARTS):
        ticket_type = "question"
    elif any(word in lowered for word in _CHORE_WORDS):
        ticket_type = "chore"
    elif any(word in lowered for word in _FEATURE_WORDS):
        ticket_type = "feature"
    component = next((c for word, c in _COMPONENTS.items() if word in lowered), "general")
    severity = "high" if any(w in lowered for w in _HIGH_SEVERITY_WORDS) else "low"
    return {
        "type": ticket_type,
        "severity": severity,
        "component": component,
        "rationale": f"Fake LLM: keyword rules classified this as a {ticket_type}.",
    }


def _ticket_text(messages: Sequence[BaseMessage]) -> str:
    return next((str(m.content) for m in messages if isinstance(m, HumanMessage)), "")


def _tool_call(name: str, args: dict[str, Any], step: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"fake-{step}", "type": "tool_call"}],
    )


class FakeChatModel(BaseChatModel):
    """Keyword rules standing in for a model, so `make dev` works without AWS.

    It looks like an agent from the outside: it searches, reads what it found, then submits.
    It does not understand anything, and nothing it says is real analysis.
    """

    bound_tools: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "ticketgate-fake"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        names = tuple(convert_to_openai_tool(t)["function"]["name"] for t in tools)
        return self.model_copy(update={"bound_tools": names})

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[LanguageModelInput, Any]:
        def respond(messages: Any) -> BaseModel:
            model: type[BaseModel] = schema
            return model.model_validate(_classify(_ticket_text(messages)))

        return RunnableLambda(respond)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if "submit_investigation" in self.bound_tools:
            message = self._investigate(messages)
        else:
            message = AIMessage(content=self._draft(_ticket_text(messages)))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _investigate(self, messages: Sequence[BaseMessage]) -> AIMessage:
        ticket = _ticket_text(messages).lower()
        results = [m for m in messages if isinstance(m, ToolMessage)]
        step = len(results)
        if step == 0:
            term = next((t for t in _SEARCH_TERMS if t in ticket), "def ")
            return _tool_call("search_code", {"query": term}, step)
        if step == 1:
            hit = re.match(r"^([^:\n]+):\d+:", str(results[0].content))
            if hit:
                return _tool_call("read_file", {"path": hit.group(1)}, step)
            return _tool_call("list_files", {"path": "."}, step)
        return _tool_call("submit_investigation", self._conclusion(ticket), step)

    @staticmethod
    def _conclusion(ticket: str) -> dict[str, Any]:
        if "discount" in ticket:
            return {
                "summary": "`apply_discount` divides 100 by the percent with integer division, "
                "which rounds the rate and crashes at 0%.",
                "plan": [
                    "Compute the discount as `subtotal_cents * percent // 100`.",
                    "Add tests for 0%, 30% and 100%.",
                ],
                "diff": _DISCOUNT_FIX,
            }
        return {
            "summary": "This needs a design decision before any code changes.",
            "plan": [
                "Agree the expected behaviour with the ticket author.",
                "Add the change behind a test that captures that behaviour.",
            ],
            "diff": None,
        }

    @staticmethod
    def _draft(ticket: str) -> str:
        if "currency" in ticket.lower():
            return (
                "The default currency is set by `DEFAULT_CURRENCY` in `src/invoicing/settings.py`."
            )
        return "Thanks for raising this. It has been triaged and queued for the team."
