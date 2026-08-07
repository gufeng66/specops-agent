from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class EvidenceKind(str, Enum):
    SOURCE = "source"
    INFERENCE = "inference"


class MeetingBrief(BaseModel):
    goal: str = ""
    stakeholders: list[str] = Field(default_factory=list)
    meeting_date: str = ""
    context: str = ""


class ActionItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = ""
    description: str = ""
    owner: str = ""
    deadline: str = ""
    acceptance_criteria: str = ""
    evidence_kind: EvidenceKind = EvidenceKind.SOURCE
    evidence_quote: str = ""
    inference_reason: str = ""
    priority: str = "medium"

    @field_validator("title", "description", "owner", "deadline", "acceptance_criteria", mode="before")
    @classmethod
    def strip_strings(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class TaskDraft(BaseModel):
    items: list[ActionItem] = Field(default_factory=list)
    version: int = 1


class IssueSeverity(str, Enum):
    ERROR = "error"
    ADVISORY = "advisory"


class ValidationIssue(BaseModel):
    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    item_id: str | None = None
    field: str | None = None


class ValidationResult(BaseModel):
    valid: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)
    repair_attempts: int = 0

    @property
    def error_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.ERROR]

    @property
    def advisory_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.ADVISORY]

    @property
    def has_blocking_errors(self) -> bool:
        return len(self.error_issues) > 0


class ThreadStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"


class ReviewInfo(BaseModel):
    feedback: str = ""
    revision_scope: Literal["item", "document"] = "item"
    target_item_id: str | None = None
    approved: bool = False


class ThreadSnapshot(BaseModel):
    thread_id: str
    status: ThreadStatus = ThreadStatus.IDLE
    source_text: str = ""
    meeting_brief: MeetingBrief = Field(default_factory=MeetingBrief)
    task_draft: TaskDraft = Field(default_factory=TaskDraft)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    run_id: str = ""
    interrupt_id: str = ""
    review: ReviewInfo = Field(default_factory=ReviewInfo)
    retrieve_results: list[dict] = Field(default_factory=list)
    markdown_output: str = ""
    sync_status: str = "not_synced"  # mock only — no real external sync
    graph_ns: str = ""  # LangGraph checkpointer thread key (stable across HITL resume)
    created_at: str = ""
    updated_at: str = ""


class ActionItemList(BaseModel):
    """Structured wrapper for LLM decompose output."""

    items: list[ActionItem] = Field(default_factory=list)


class MeetingBriefExtract(BaseModel):
    """Structured wrapper for LLM intake output."""

    brief: MeetingBrief
