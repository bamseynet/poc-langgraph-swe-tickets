"""The investigate step: a tool-using agent, built as its own small graph.

    start -> agent -> tools -> agent ...
               |-> finish       the model called submit_investigation
               |-> nudge        the model replied in text, so ask it to conclude
               |-> give_up      the step budget ran out

Claude Sonnet 5.5 and Opus 5.5 accept only `auto` and `none` for tool_choice, so the agent
cannot be forced to call submit_investigation. The nudge and the budget replace that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import RetryPolicy
from pydantic import BaseModel, Field, ValidationError, field_validator

from ticketgate.models import Investigation, Ticket, ToolCallRecord, TriageResult
from ticketgate.prompts import INVESTIGATOR_SYSTEM, NUDGE, render_investigation_request
from ticketgate.state import InvestigatorState
from ticketgate.tools import check_patch, make_read_only_tools

SUBMIT_TOOL = "submit_investigation"


class InvestigationSubmission(BaseModel):
    summary: str = Field(description="What is wrong or what is needed, and where.")
    plan: list[str] = Field(description="Ordered steps to make the change.")
    diff: str | None = Field(
        default=None,
        description="Unified diff relative to the repository root, or null if not feasible.",
    )

    @field_validator("diff")
    @classmethod
    def _normalise_diff(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        # `git apply` calls a patch corrupt without its final newline.
        return value if value.endswith("\n") else f"{value}\n"


@tool(SUBMIT_TOOL, args_schema=InvestigationSubmission)
def _submit_investigation(summary: str, plan: list[str], diff: str | None) -> str:
    """Submit your conclusion. This ends the investigation."""
    # Never executed: the graph routes this call to the finish node instead of ToolNode.
    raise NotImplementedError


def _last_ai_message(state: InvestigatorState) -> AIMessage:
    message = state["messages"][-1]
    assert isinstance(message, AIMessage)
    return message


def _tool_trace(messages: list[BaseMessage]) -> list[ToolCallRecord]:
    return [
        ToolCallRecord(tool=call["name"], args=call["args"])
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] != SUBMIT_TOOL
    ]


def build_investigator(
    llm: BaseChatModel, root: Path, *, max_steps: int, retry: RetryPolicy | None = None
) -> CompiledStateGraph[InvestigatorState, Any, InvestigatorState, InvestigatorState]:
    read_tools = make_read_only_tools(root)
    model = llm.bind_tools([*read_tools, _submit_investigation])

    def start(state: InvestigatorState) -> InvestigatorState:
        ticket = Ticket.model_validate(state["ticket"])
        triage = TriageResult.model_validate(state["triage"])
        return {
            "messages": [
                SystemMessage(content=INVESTIGATOR_SYSTEM),
                HumanMessage(content=render_investigation_request(ticket, triage)),
            ],
            "steps": 0,
        }

    def agent(state: InvestigatorState) -> InvestigatorState:
        return {
            "messages": [model.invoke(state["messages"])],
            "steps": state["steps"] + 1,
        }

    def route(state: InvestigatorState) -> Literal["finish", "tools", "nudge", "give_up"]:
        names = [call["name"] for call in _last_ai_message(state).tool_calls]
        if SUBMIT_TOOL in names:
            return "finish"
        if state["steps"] >= max_steps:
            return "give_up"
        return "tools" if names else "nudge"

    def nudge(state: InvestigatorState) -> InvestigatorState:
        return {"messages": [HumanMessage(content=NUDGE)]}

    def _stopped(state: InvestigatorState, reason: str) -> InvestigatorState:
        last_said = _last_ai_message(state).text
        summary = f"{reason} Last message: {last_said}" if last_said else reason
        investigation = Investigation(
            summary=summary, complete=False, tool_calls=_tool_trace(state["messages"])
        )
        return {"investigation": investigation.model_dump(mode="json")}

    def finish(state: InvestigatorState) -> InvestigatorState:
        call = next(c for c in _last_ai_message(state).tool_calls if c["name"] == SUBMIT_TOOL)
        try:
            submission = InvestigationSubmission.model_validate(call["args"])
        except ValidationError as exc:
            return _stopped(
                state, f"The agent's submission was invalid: {exc.error_count()} error(s)."
            )
        investigation = Investigation(
            summary=submission.summary,
            plan=submission.plan,
            diff=submission.diff,
            patch_applies=check_patch(root, submission.diff) if submission.diff else None,
            tool_calls=_tool_trace(state["messages"]),
        )
        return {"investigation": investigation.model_dump(mode="json")}

    def give_up(state: InvestigatorState) -> InvestigatorState:
        return _stopped(state, f"The agent reached its limit of {max_steps} steps.")

    builder = StateGraph(InvestigatorState)
    builder.add_node("start", start)
    builder.add_node("agent", agent, retry_policy=retry)
    # A failing tool call goes back to the model as a message. Raising would stop the ticket,
    # and every retry would repeat the same call.
    builder.add_node("tools", ToolNode(read_tools, handle_tool_errors=True))
    builder.add_node("nudge", nudge)
    builder.add_node("finish", finish)
    builder.add_node("give_up", give_up)
    builder.add_edge(START, "start")
    builder.add_edge("start", "agent")
    builder.add_conditional_edges("agent", route, ["finish", "tools", "nudge", "give_up"])
    builder.add_edge("tools", "agent")
    builder.add_edge("nudge", "agent")
    builder.add_edge("finish", END)
    builder.add_edge("give_up", END)
    return builder.compile()
