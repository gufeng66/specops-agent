from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ChatStreamRequest(BaseModel):
    source_text: str = Field(min_length=1)
    thread_id: str | None = None
    request_id: str = Field(min_length=1)
    clarify_response: str | None = None


class ResumeRequest(BaseModel):
    request_id: str = Field(min_length=1)
    revision_scope: Literal["item", "document"] = "item"
    target_item_id: str | None = None
    feedback: str = ""
    approve: bool = False

    @model_validator(mode="after")
    def check_item_scope(self) -> ResumeRequest:
        if self.revision_scope == "item" and not self.approve and not self.target_item_id:
            raise ValueError("target_item_id is required when revision_scope is item")
        return self
