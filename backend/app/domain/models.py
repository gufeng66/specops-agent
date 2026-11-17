"""领域模型：会议摘要、行动项、校验结果、线程快照。

约定：
- `evidence_quote` 必须是原文连续子串（source 证据），不能改写后当引用。
- `owner_suggestions` 只服务 HITL 分配面板，禁止自动拷贝到 `primary_owner`。
- JSON 导出用 `ThreadSnapshot.export_dict()` 剥离建议字段；GET 线程快照仍保留以便 UI 恢复。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

# 业务快照结构版本。写入路径统一拒绝高于本常量的快照，避免未来版本的数据被
# 旧代码「读得懂但语义不对」地覆盖回库。只升不降：加字段时递增并写迁移。
SNAPSHOT_SCHEMA_VERSION = 1


class EvidenceKind(str, Enum):
    """证据类型。source=原文引用；inference=无法截取原文时的推断理由。"""

    SOURCE = "source"
    INFERENCE = "inference"


class MeetingBrief(BaseModel):
    """intake 产出的会议摘要。goal 为空时触发 clarify HITL。"""

    goal: str = ""
    stakeholders: list[str] = Field(default_factory=list)
    discovered_names: list[str] = Field(default_factory=list)
    meeting_date: str = ""
    context: str = ""


class OwnerCandidate(BaseModel):
    """负责人建议（建议通道）。

    从不自动写入 `ActionItem.primary_owner`。代词消解时 `name` 可以不出现在原文，
    但 `evidence_kind=source` 时 `evidence_quote` 必须是「缺负责人」那句原文。
    `confidence` 仅存原始分，UI 映射成低/中/高，不当门槛、不用于自动选人。
    """

    name: str = ""
    role: Literal["primary", "secondary"] = "primary"
    confidence: float = 0.0
    reason: str = ""
    evidence_kind: EvidenceKind = EvidenceKind.SOURCE
    evidence_quote: str = ""
    inference_reason: str = ""

    @field_validator("name", "reason", "evidence_quote", "inference_reason", mode="before")
    @classmethod
    def strip_strings(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: object) -> object:
        # bool 是 int 子类，必须先排除，否则 True 会变成 1.0。
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return 0.0
        return max(0.0, min(1.0, float(v)))


class ActionItem(BaseModel):
    """一条可执行任务。主责一人；次责可多人。

    `unresolved_ref` 非空且主责为空 → UNRESOLVED_REFERENCE，必须进人工分配。
    `owner_suggestions` 由 propose_owners 填写，decompose 输出必须清空。
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = ""
    description: str = ""
    primary_owner: str = ""
    secondary_owners: list[str] = Field(default_factory=list)
    deadline: str = ""
    acceptance_criteria: str = ""
    acceptance_origin: Literal["meeting", "system", "human"] = "meeting"
    evidence_kind: EvidenceKind = EvidenceKind.SOURCE
    evidence_quote: str = ""
    inference_reason: str = ""
    unresolved_ref: str = ""
    priority: str = "medium"
    owner_suggestions: list[OwnerCandidate] = Field(default_factory=list)

    @field_validator(
        "title",
        "description",
        "primary_owner",
        "deadline",
        "acceptance_criteria",
        "unresolved_ref",
        mode="before",
    )
    @classmethod
    def strip_strings(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("secondary_owners", mode="before")
    @classmethod
    def strip_secondary_owners(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return [item.strip() for item in v if isinstance(item, str) and item.strip()]


class TaskDraft(BaseModel):
    """当前线程的任务清单。version 在修复/删除/分配后递增，便于前端感知变化。"""

    items: list[ActionItem] = Field(default_factory=list)
    version: int = 1


class IssueSeverity(str, Enum):
    """error 阻断进入审核（负责人类会先走分配）；advisory 不阻断。"""

    ERROR = "error"
    ADVISORY = "advisory"


class ValidationIssue(BaseModel):
    """单条校验问题。code 与 OWNER_ERROR_CODES / 修复逻辑对齐。"""

    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    item_id: str | None = None
    field: str | None = None


class ValidationResult(BaseModel):
    """一次 validate_task_draft 的汇总。repair_attempts 记录工具循环或规则修复轮次。"""

    valid: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)
    repair_attempts: int = 0

    @property
    def error_issues(self) -> list[ValidationIssue]:
        """阻断类问题，含 EMPTY_TASK_LIST 和负责人错误。"""
        return [i for i in self.issues if i.severity == IssueSeverity.ERROR]

    @property
    def advisory_issues(self) -> list[ValidationIssue]:
        """不阻断，如缺截止日期、套话验收。"""
        return [i for i in self.issues if i.severity == IssueSeverity.ADVISORY]

    @property
    def has_blocking_errors(self) -> bool:
        """是否还有 error。负责人类 error 不阻断 finalize，只阻断自动填人。"""
        return len(self.error_issues) > 0


class ExtractionInfo(BaseModel):
    """本次分块抽取的覆盖情况。是部分失败告警的权威来源。

    `partial`：有块失败但也有块成功，结果不完整；
    `degraded`：所有块都失败，只剩规则底稿。
    两者都要在审核面板和导出里明示，不能伪装成完整的模型结果。
    """

    chunks_total: int = 0
    chunks_ok: int = 0
    chunks_empty: int = 0
    chunks_failed: int = 0
    partial: bool = False
    degraded: bool = False
    detail: str = ""


class ThreadStatus(str, Enum):
    """HTTP 层线程状态。三种 HITL（澄清/分配/审核）都记成 awaiting_review，前端靠 interrupt.type 区分。"""

    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewInfo(BaseModel):
    """最近一次审核动作的记录，随快照保存。"""

    feedback: str = ""
    revision_scope: Literal["item", "document", "drop"] = "item"
    target_item_id: str | None = None
    approved: bool = False
    dropped_item_ids: list[str] = Field(default_factory=list)
    reviewed_task_count: int = 0
    edited_item_ids: list[str] = Field(default_factory=list)
    dropped_task_count: int = 0
    # 人工放行记录。operator_claim 是用户自述，不代表已认证身份。
    override_reason: str = ""
    override_codes: list[str] = Field(default_factory=list)
    override_at: str = ""
    operator_claim: str = ""


class ThreadSnapshot(BaseModel):
    """一次分析会话的完整照片，落 SQLite；LangGraph checkpoint 另存图游标。"""

    thread_id: str
    status: ThreadStatus = ThreadStatus.IDLE
    source_text: str = ""
    revision_notes: list[str] = Field(default_factory=list)
    meeting_brief: MeetingBrief = Field(default_factory=MeetingBrief)
    task_draft: TaskDraft = Field(default_factory=TaskDraft)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    extraction: ExtractionInfo = Field(default_factory=ExtractionInfo)
    run_id: str = ""
    interrupt_id: str = ""
    interrupt_payload: dict | None = None
    review: ReviewInfo = Field(default_factory=ReviewInfo)
    markdown_output: str = ""
    token_in: int = 0
    token_out: int = 0
    latency_ms: int = 0  # 成功模型调用耗时之和，不含失败尝试和退避
    wall_ms: int = 0  # 本次执行段墙钟，不含人工审核停留
    llm_calls: int = 0  # 本执行段发出的 HTTP 尝试数，含传输/schema 重试
    transport_retries: int = 0  # 429/5xx/传输错误引起的重试次数
    schema_retries: int = 0  # JSON/schema 校验失败引起的重试次数
    llm_failures: int = 0  # 失败的 HTTP 尝试数，含重试中间的失败
    graph_ns: str = ""  # checkpointer 线程键，HITL resume 必须稳定
    checkpoint_id: str = ""  # 最近一次对齐到的 checkpoint，跨库恢复时用于发现失配
    prompt_version: str = ""
    schema_version: int = 1  # 数据结构兼容版本，只用于读旧数据
    revision: int = 0  # 同一线程的逻辑版本，写入时做 CAS
    created_at: str = ""
    updated_at: str = ""

    def export_dict(self) -> dict:
        """交付用 JSON：递归去掉 HITL 专用建议，绝不改动恢复快照。"""
        data = self.model_dump(mode="json")

        def strip_suggestions(value: object) -> None:
            if isinstance(value, dict):
                value.pop("owner_suggestions", None)
                for child in value.values():
                    strip_suggestions(child)
            elif isinstance(value, list):
                for child in value:
                    strip_suggestions(child)

        strip_suggestions(data)
        return data


class ActionItemList(BaseModel):
    """DeepSeek decompose 的 JSON 外壳，对应 {"items": [...]}。"""

    items: list[ActionItem] = Field(default_factory=list)


class MeetingBriefExtract(BaseModel):
    """DeepSeek intake 的 JSON 外壳，对应 {"brief": {...}}。"""

    brief: MeetingBrief


class ItemOwnerSuggestions(BaseModel):
    """propose_owners 单条任务的建议包，用 draft 里的真实 item_id 对齐。"""

    item_id: str
    suggestions: list[OwnerCandidate] = Field(default_factory=list)


class OwnerSuggestionBatch(BaseModel):
    """DeepSeek propose_owners 的 JSON 外壳。解析失败时节点返回空建议，不卡图。"""

    items: list[ItemOwnerSuggestions] = Field(default_factory=list)
