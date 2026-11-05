"""状态机路由单测：四个条件边的边界，以及 HITL 状态标记辅助函数。

路由函数只读 state，不读库、不调模型，所以这里直接构造最小 state 字典。
propose_owners 分支单独用 monkeypatch 关掉 llm_client，避免本地 .env 里的 Key 把
route_after_validate_repair 强制导向 propose_owners。
"""

import pytest

from app.agent.nodes import (
    MAX_ASSIGN,
    route_after_assign,
    route_after_propose,
    route_after_review,
    route_after_validate_repair,
)
from app.domain.models import (
    IssueSeverity,
    ValidationIssue,
    ValidationResult,
)
from app.domain.validation import OWNER_ERROR_CODES, has_owner_errors

SOURCE_TEXT = """会议目标：发布准备
参会人：张伟、李娜
认证模块这周要完成代码评审。
"""


def _validation(*issues: ValidationIssue) -> dict:
    """把问题列表包成 state 里的 validation 字段。"""
    return ValidationResult(issues=list(issues)).model_dump(mode="json")


def _issue(
    code: str,
    severity: IssueSeverity = IssueSeverity.ERROR,
    item_id: str | None = "item-1",
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=f"{code} 的说明",
        severity=severity,
        item_id=item_id,
    )


def _state(**overrides) -> dict:
    base: dict = {
        "source_text": SOURCE_TEXT,
        "meeting_brief": {"goal": "发布准备", "stakeholders": ["张伟", "李娜"]},
        "task_draft": {"items": [], "version": 1},
        "validation": _validation(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def llm_off(monkeypatch):
    """LLM_PROVIDER=none：llm_client.enabled 为 False，走纯规则语义。"""

    class _Disabled:
        enabled = False

    monkeypatch.setattr("app.agent.nodes.llm_client", _Disabled())


@pytest.fixture
def llm_on(monkeypatch):
    """模拟有 Key：enabled 为 True，路由应无视 owner ERROR 直接进 propose_owners。"""

    class _Enabled:
        enabled = True

    monkeypatch.setattr("app.agent.nodes.llm_client", _Enabled())


# --------------------------------------------------------------------------
# route_after_validate_repair
# --------------------------------------------------------------------------


def test_validate_repair_owner_error_goes_propose(llm_off):
    """MISSING_OWNER 属负责人类 ERROR，必须先进 propose_owners。"""
    state = _state(validation=_validation(_issue("MISSING_OWNER")))
    assert route_after_validate_repair(state) == "propose_owners"


def test_validate_repair_secondary_error_goes_propose(llm_off):
    """INVALID_SECONDARY 同属负责人类，路由结果一致。"""
    state = _state(validation=_validation(_issue("INVALID_SECONDARY")))
    assert route_after_validate_repair(state) == "propose_owners"


def test_validate_repair_clean_goes_review(llm_off):
    """无任何问题时直达 review。"""
    assert route_after_validate_repair(_state()) == "review"


def test_validate_repair_non_owner_error_goes_review(llm_off):
    """非负责人类 ERROR（如缺验收标准）不触发分配，由规则修复处理。"""
    state = _state(validation=_validation(_issue("MISSING_ACCEPTANCE")))
    assert route_after_validate_repair(state) == "review"


def test_validate_repair_advisory_only_goes_review(llm_off):
    """advisory 不算 owner ERROR。"""
    state = _state(
        validation=_validation(_issue("MISSING_DEADLINE", severity=IssueSeverity.ADVISORY))
    )
    assert route_after_validate_repair(state) == "review"


def test_validate_repair_llm_enabled_always_propose(llm_on):
    """有 Key 时即使零问题也进 propose_owners，用来产出次责建议。"""
    assert route_after_validate_repair(_state()) == "propose_owners"


def test_validate_repair_missing_validation_field_defaults_to_review(llm_off):
    """validation 缺省时按空结果处理，不抛异常。"""
    assert route_after_validate_repair({"source_text": SOURCE_TEXT}) == "review"


# --------------------------------------------------------------------------
# route_after_propose
# --------------------------------------------------------------------------


def test_propose_still_owner_error_goes_assign(llm_off):
    """建议 ≠ 写入：仍有 owner ERROR 就必须人工分配。"""
    state = _state(validation=_validation(_issue("INVALID_PRIMARY")))
    assert route_after_propose(state) == "assign_owners"


def test_propose_resolved_goes_review(llm_on):
    """主责已合法（模型只给次责建议）时带建议进 review。"""
    assert route_after_propose(_state()) == "review"


def test_propose_ignores_llm_enabled_flag(llm_on):
    """route_after_propose 只看 owner ERROR，不看 llm_client.enabled。"""
    state = _state(validation=_validation(_issue("MISSING_ACCEPTANCE")))
    assert route_after_propose(state) == "review"


def test_propose_advisory_goes_review(llm_off):
    state = _state(
        validation=_validation(_issue("MISSING_DEADLINE", severity=IssueSeverity.ADVISORY))
    )
    assert route_after_propose(state) == "review"


# --------------------------------------------------------------------------
# route_after_assign
# --------------------------------------------------------------------------


def test_assign_reinterrupts_when_node_says_assign(llm_off):
    """assign_owners 未到上限且仍非法时自己写 route=assign_owners。"""
    assert route_after_assign(_state(assign_action={"route": "assign_owners"})) == "assign_owners"


def test_assign_goes_review_when_node_says_review(llm_off):
    """合法或已到 MAX_ASSIGN 上限，节点写 route=review。"""
    assert route_after_assign(_state(assign_action={"route": "review"})) == "review"


def test_assign_missing_action_falls_back_to_review(llm_off):
    """没有 assign_action（异常路径）时默认进 review，不允许死循环分配。"""
    assert route_after_assign(_state()) == "review"


def test_assign_empty_route_falls_back_to_review(llm_off):
    assert route_after_assign(_state(assign_action={"route": ""})) == "review"


def test_assign_unknown_route_falls_back_to_review(llm_off):
    """非白名单路由不会把流程带出 assign_owners。"""
    assert route_after_assign(_state(assign_action={"route": "decompose"})) == "review"


def test_assign_max_attempt_constant():
    """上限常量作为路由语义的一部分被固定下来，改动需要同步改测试。"""
    assert MAX_ASSIGN == 3


# --------------------------------------------------------------------------
# route_after_review
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    ["finalize", "intake", "validate_repair", "review"],
)
def test_review_passes_through_supported_routes(llm_off, route):
    """节点写入的四个合法路由原样透传。"""
    assert route_after_review(_state(review_action={"route": route})) == route


def test_review_approve_goes_finalize(llm_off):
    assert route_after_review(_state(review_action={"route": "finalize"})) == "finalize"


def test_review_document_revision_goes_intake(llm_off):
    """全文重跑走 intake，而不是 validate_repair。"""
    assert route_after_review(_state(review_action={"route": "intake"})) == "intake"


def test_review_item_revision_goes_validate_repair(llm_off):
    assert route_after_review(_state(review_action={"route": "validate_repair"})) == (
        "validate_repair"
    )


def test_review_drop_goes_review(llm_off):
    """删除单条后原地再审，不重新拆解。"""
    assert route_after_review(_state(review_action={"route": "review"})) == "review"


def test_review_missing_action_stays_in_review(llm_off):
    """缺省和未知路由回到审核，避免意外批准。"""
    assert route_after_review(_state()) == "review"


def test_review_unknown_action_stays_in_review(llm_off):
    assert route_after_review(_state(review_action={"route": "decompose"})) == "review"


# --------------------------------------------------------------------------
# HITL 状态标记辅助函数
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(OWNER_ERROR_CODES))
def test_has_owner_errors_recognizes_every_owner_code(code):
    """OWNER_ERROR_CODES 里每个码都必须被识别，路由依赖这个集合。"""
    assert has_owner_errors(ValidationResult(issues=[_issue(code)]))


@pytest.mark.parametrize(
    "code",
    ["MISSING_TITLE", "MISSING_ACCEPTANCE", "MISSING_EVIDENCE_QUOTE", "EMPTY_TASK_LIST"],
)
def test_has_owner_errors_ignores_non_owner_errors(code):
    """非负责人类 ERROR 交给规则修复，不应触发人工分配。"""
    assert not has_owner_errors(ValidationResult(issues=[_issue(code)]))


@pytest.mark.parametrize("code", sorted(OWNER_ERROR_CODES))
def test_advisory_owner_code_is_not_owner_error(code):
    """即便码名在 OWNER_ERROR_CODES 内，降级成 advisory 后也不算 owner ERROR。"""
    result = ValidationResult(issues=[_issue(code, severity=IssueSeverity.ADVISORY)])
    assert not has_owner_errors(result)
    assert result.error_issues == []
    assert len(result.advisory_issues) == 1


def test_has_owner_errors_mixed_issues():
    """混合问题里只要有一条 owner ERROR 就为真。"""
    result = ValidationResult(
        issues=[
            _issue("MISSING_ACCEPTANCE"),
            _issue("MISSING_DEADLINE", severity=IssueSeverity.ADVISORY),
            _issue("UNRESOLVED_REFERENCE"),
        ]
    )
    assert has_owner_errors(result)
    assert [i.code for i in result.error_issues] == ["MISSING_ACCEPTANCE", "UNRESOLVED_REFERENCE"]


def test_has_owner_errors_empty_result():
    assert not has_owner_errors(ValidationResult())
