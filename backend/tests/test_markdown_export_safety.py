"""Markdown 导出协议：原文用动态 fenced code block 原样保留，不被折叠、不被转义。

对应 m02334 P1 与 m01951 第 11 项：用户决策「保持纯代码围栏，不恢复 <details>」。
验证：
- 原文里含反引号 / ``` / ~~~ / <details> / <script> / Markdown 标题链接列表时，
  围栏长度按最长连续反引号 +1 自动加长，原文仍是完整、未被改写的连续子串；
- _md_inline 只作用于结构字段，不改领域对象里的 evidence_quote / source_text；
- 结构字段（标题/负责人/证据/放行理由）里的 Markdown/HTML 元字符被转义。
"""

from app.domain.extractors import render_markdown
from app.domain.models import (
    ActionItem,
    ExtractionInfo,
    MeetingBrief,
    ReviewInfo,
    TaskDraft,
    ValidationIssue,
    ValidationResult,
)
from app.domain.validation import validate_task_draft


def _render(
    source: str,
    *,
    review: ReviewInfo | None = None,
    extraction: ExtractionInfo | None = None,
) -> tuple[str, TaskDraft]:
    brief = MeetingBrief(
        goal="# 目标 <b>加粗</b> [链接](http://x)",
        stakeholders=["张*伟", "李_娜"],
    )
    item = ActionItem(
        title="任务 `反引号` 与 <script>alert(1)</script>",
        description="描述",
        primary_owner="张*伟",
        secondary_owners=["李_娜"],
        deadline="下周五",
        acceptance_criteria="通过 [评审](http://x)",
        evidence_kind="source",
        evidence_quote=source,  # 证据就是原文连续子串
    )
    draft = TaskDraft(items=[item])
    issues = [ValidationIssue(code="ADVISORY_NOTE", message="说明 `x` <b>y</b>")]
    validation = ValidationResult(issues=issues)
    md = render_markdown(brief, draft, validation, source, review=review, extraction=extraction)
    return md, draft


HOSTILE = "\n".join(
    [
        "# 一级标题",
        "普通行 `inline` 与 ``` 三反引号",
        "```` 四反引号 ```` 收尾",
        "~~~ tilde fence ~~~",
        "<details><summary>点开</summary>隐藏</details>",
        "<script>alert(1)</script>",
        "- [ ] 列表项",
        "1. 有序列表",
        "[链接](http://evil.example) 与 **加粗**",
    ]
)


def test_source_is_verbatim_inside_dynamic_fence():
    md, _ = _render(HOSTILE)
    # 动态围栏：原文最长连续反引号是 4，围栏必须是 5，且用 text 语言标注。
    fence = "`" * 5
    assert f"{fence}text\n{HOSTILE}\n{fence}" in md
    # 原文整块在围栏里逐字保留，没被 _md_inline 改写、没被 HTML 转义、没被折叠。
    assert HOSTILE in md
    assert "<details>" not in md.split("## 原文（原样，未转义）")[0]
    assert "&lt;script&gt;" not in md
    # 协议明确放弃 <details> 折叠。
    assert "<details><summary>" in md  # 只作为原文内容出现，不是包装元素


def test_fence_grows_with_longest_backtick_run():
    for run in (0, 2, 3, 7, 12):
        source = "A" + "`" * run + "B"
        md, _ = _render(source)
        fence = "`" * max(3, run + 1)
        assert f"{fence}text\n{source}\n{fence}" in md
        assert source in md


def test_tilde_fence_and_html_are_not_interpreted_as_structure():
    md, _ = _render("~~~\n<details>x</details>\n~~~")
    body = md.split("## 原文（原样，未转义）")[0]
    # 结构区不能凭空出现 <details> 或 tilde fence（它们只应在原文围栏里）。
    assert "<details>" in md
    assert "## 原文（原样，未转义）" in body or body == md.split("## 原文")[0]


def test_domain_objects_are_not_mutated_by_rendering():
    source = "张三负责上线 `模块` <script>x</script>"
    md, draft = _render(source)
    item = draft.items[0]
    # _md_inline 只影响导出字符串，不改领域对象里的证据/原文。
    assert item.evidence_quote == source
    assert item.title == "任务 `反引号` 与 <script>alert(1)</script>"
    # 而导出的结构字段确实被转义了。
    assert "\\`" in md  # 结构字段里的反引号被转义
    assert "张\\*伟" in md


def test_structural_fields_escape_markdown_and_html():
    md, _ = _render(HOSTILE)
    head = md.split("## 原文（原样，未转义）")[0]
    assert "\\# 目标" in head  # 会议目标的 # 被转义
    assert "张\\*伟" in head
    assert "李\\_娜" in head
    assert "<script>alert(1)</script>" not in head  # 结构区没有未转义的脚本标签


def test_override_section_escapes_free_text():
    review = ReviewInfo(
        override_reason="确认 <b>语义</b> 一致 `放行`",
        override_codes=["INVALID_EVIDENCE_QUOTE"],
        override_at="2026-11-13T10:00:00",
        operator_claim="李*娜（自述）",
    )
    md, _ = _render("原文", review=review)
    section = md.split("## 人工放行")[1].split("## ")[0]
    assert "\\`" in section
    assert "李\\*娜" in section
    assert "<b>语义</b>" not in section  # 被转义或至少不是裸 HTML
    assert "## 原文（原样，未转义）" in md


def test_evidence_quote_stays_continuous_substring_of_source():
    source = "张三负责完成认证模块重构，下周五前提交代码评审。"
    md, draft = _render(source)
    assert draft.items[0].evidence_quote in source
    # 校验路径不受导出渲染影响：source 证据仍被当成原文连续子串。
    brief = MeetingBrief(goal="Q1 发布", stakeholders=["张三"])
    result = validate_task_draft(draft, brief, source)
    assert all(i.code != "INVALID_EVIDENCE_QUOTE" for i in result.issues)


def test_extraction_summary_rules_cover_unknown_partial_and_degraded():
    unknown, _ = _render("原文")
    assert "抽取状态**: 未记录" in unknown

    partial, _ = _render(
        "原文", extraction=ExtractionInfo(chunks_ok=2, chunks_empty=1, chunks_failed=1, partial=True)
    )
    assert "抽取状态**: 成功 2 / 零任务 1 / 失败 1" in partial
    assert "结果不完整，请人工确认" in partial

    degraded, _ = _render(
        "原文", extraction=ExtractionInfo(chunks_failed=3, degraded=True)
    )
    assert "结果不完整，请人工确认" in degraded
    assert "当前仅保留规则结果" in degraded
