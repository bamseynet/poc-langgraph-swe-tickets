"""Domain models shared by every layer.

Graph state holds these as plain JSON (`model_dump(mode="json")`), so a checkpoint written
today stays readable after these classes are renamed or moved.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TicketType = Literal["bug", "feature", "chore", "question"]
Severity = Literal["low", "medium", "high", "critical"]
DecisionAction = Literal["approve", "edit", "reject"]

# GitHub rejects comments over 65,536 characters. Leave room for the idempotency marker.
MAX_COMMENT_CHARS = 60_000
MAX_COMPONENT_CHARS = 30


class TicketRef(BaseModel):
    """Identifies a ticket in its source system."""

    model_config = ConfigDict(frozen=True)

    source: str
    project: str
    key: str

    @property
    def thread_id(self) -> str:
        """One ticket is one LangGraph thread, so one ticket gets one approval."""
        return f"{self.source}:{self.project}#{self.key}"


class Ticket(BaseModel):
    """A ticket normalised from any `TicketSource`."""

    ref: TicketRef
    title: str
    body: str = ""
    author: str = "unknown"
    labels: list[str] = Field(default_factory=list)
    url: str = ""
    created_at: datetime


class TriageResult(BaseModel):
    """Classification of a software engineering ticket."""

    type: TicketType = Field(
        description="bug: something is broken. feature: new behaviour is requested. "
        "chore: maintenance with no behaviour change. question: the author wants an answer."
    )
    severity: Severity = Field(description="Impact on users if nothing is done.")
    component: str = Field(description="Short name of the affected part of the system.")
    rationale: str = Field(description="One or two sentences justifying the classification.")

    @field_validator("component")
    @classmethod
    def _slugify_component(cls, value: str) -> str:
        # Normalised here, not with maxLength or pattern, because this schema is also sent
        # to Bedrock structured output, which rejects string length constraints.
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug[:MAX_COMPONENT_CHARS].strip("-") or "unknown"

    def labels(self) -> list[str]:
        return [
            f"type:{self.type}",
            f"severity:{self.severity}",
            f"component:{self.component}",
        ]


class ToolCallRecord(BaseModel):
    """One read-only tool call the investigator made, as shown to the reviewer."""

    tool: str
    args: dict[str, object]


class Investigation(BaseModel):
    """What the investigator hands to the human reviewer."""

    summary: str
    plan: list[str] = Field(default_factory=list)
    diff: str | None = None
    # None when the check could not run or there is no diff.
    patch_applies: bool | None = None
    # False when the agent ran out of steps. The ticket still goes to the gate.
    complete: bool = True
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class Proposal(BaseModel):
    """The exact write-back a reviewer is asked to approve."""

    comment: str
    labels: list[str]


class ApprovalDecision(BaseModel):
    """The value a human passes to `Command(resume=...)`."""

    decision_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    action: DecisionAction
    actor: str
    reason: str | None = None
    edited_comment: str | None = None
    edited_labels: list[str] | None = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("actor")
    @classmethod
    def _actor_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actor must not be blank")
        return value.strip()

    @field_validator("edited_comment")
    @classmethod
    def _comment_fits(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("edited_comment must not be blank")
        if value is not None and len(value) > MAX_COMMENT_CHARS:
            raise ValueError(f"edited_comment must be at most {MAX_COMMENT_CHARS} characters")
        return value

    @model_validator(mode="after")
    def _action_has_required_fields(self) -> Self:
        has_edits = self.edited_comment is not None or self.edited_labels is not None
        if self.action == "reject" and not (self.reason and self.reason.strip()):
            raise ValueError("reject requires a reason")
        if self.action == "edit" and not has_edits:
            raise ValueError("edit requires edited_comment or edited_labels")
        if self.action != "edit" and has_edits:
            raise ValueError("only the edit action may carry edits")
        return self


class WriteBackResult(BaseModel):
    """What the writer did on the source system."""

    comment_url: str | None = None
    labels: list[str] = Field(default_factory=list)
    # True when an earlier attempt had already posted this comment.
    deduplicated: bool = False
