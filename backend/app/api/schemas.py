"""HTTP 请求体。硬上限在 schema 层拒绝超大原文；单项修订缺 target_id 时允许自然语言删除。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.config import settings
from app.domain.review_intent import looks_like_drop_feedback


class ChatStreamRequest(BaseModel):
    """开跑/澄清。硬上限在这里拒绝，软上限只提示分块。"""
    source_text: str = Field(min_length=1)
    thread_id: str | None = None
    request_id: str = Field(min_length=1)
    clarify_response: str | None = None

    @model_validator(mode="after")
    def check_source_length(self) -> ChatStreamRequest:
        limit = settings.source_hard_limit_chars
        if len(self.source_text) > limit:
            raise ValueError(f"source_text exceeds {limit} characters")
        return self


class OwnerAssignment(BaseModel):
    """人工分配一条任务。空 primary_owner 表示这条还没选。"""
    item_id: str
    primary_owner: str = ""
    secondary_owners: list[str] = Field(default_factory=list)


class ResumeRequest(BaseModel):
    """分配或审核续跑。单项修订缺 target_id 时，若反馈像删除则放行给 parse_drop_intent。"""
    request_id: str = Field(min_length=1)
    revision_scope: Literal["item", "document", "drop"] = "item"
    target_item_id: str | None = None
    feedback: str = ""
    approve: bool = False
    assignments: list[OwnerAssignment] = Field(default_factory=list)
    interrupt_type: str | None = None
    drop_item_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_item_scope(self) -> ResumeRequest:
        if self.assignments or self.approve:
            return self
        if self.revision_scope == "drop":
            return self
        if self.revision_scope == "item" and not self.target_item_id:
            if looks_like_drop_feedback(self.feedback):
                return self
            raise ValueError("target_item_id is required when revision_scope is item")
        return self
