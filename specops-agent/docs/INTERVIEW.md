# 面试要点：SpecOps Agent

## 一句话

不是 ChatBot，而是 **LangGraph 编排的 HITL Agent**：模型节点做结构化抽取，工具节点做确定性校验与 playbook 查询，人工 interrupt 后再导出。

## 模型节点 vs 工具节点

| 类型 | 节点 | 职责 |
|------|------|------|
| LLM | `intake`, `decompose`（可选 `llm_repair` 建议） | DeepSeek `chat/completions` + `response_format=json_object` + Pydantic |
| 工具 | `search_playbooks`, `validate_task_draft`, `repair_task_draft`, `export_markdown` | 本地代码，可测、可解释 |
| HITL | `clarify`, `review` | LangGraph `interrupt`，前端 resume |

失败可观测（`LLMTimeoutError` / `LLMAPIError` / `LLMParseError`），自动回退规则抽取，保证 **CI 无 Key 可跑通**。

## 证据硬约束

`evidence_kind=source` 时，工具侧强制：

```text
evidence_quote ⊆ 原文（连续子串）
```

模型可以胡说；**过不了 validate 工具就进不了干净审核态**（仍可带着 unresolved errors 进 HITL，由人裁决）。

## 与纯 ChatBot 的差别

1. **状态图而非一轮对话**：固定拓扑 + 条件边，面试官能画出图。
2. **结构化输出进领域模型**：MeetingBrief / ActionItem，不是自由文本。
3. **工具边界清晰**：检索、校验、修复、导出不交给模型「假装执行」。
4. **HITL 一等公民**：clarify/review 真正暂停图执行，resume 对齐 HTTP 契约。
5. **幂等与并发**：`request_id` 幂等、`THREAD_BUSY` 409，运维场景可讲清楚。
6. **不做假 RAG**：Playbook Lookup 是关键词工具；诚实比「企业知识库」包装更加分。

## sync_status

恒为 `not_synced`（mock）。本项目不接 Jira/飞书；面试时说明「导出是边界，外部同步是下一阶段 webhook」。

## Demo 话术（30 秒）

「我把会议纪要丢进去，LangGraph 跑 intake→decompose，DeepSeek 出结构化 JSON；接着 playbook 工具查本地清单，validate 卡住假引用；然后 interrupt 给你改一条任务或批准；最后 export markdown。没有向量库，证据必须是原文子串。」
