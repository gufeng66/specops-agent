# SpecOps Agent — Backend

LangGraph 运行时 + FastAPI SSE。详见根目录 [README.md](../README.md) 与 [docs/INTERVIEW.md](../docs/INTERVIEW.md)。

## 拓扑

`intake → clarify? → decompose → validate_repair → propose_owners? → assign_owners? → review? → finalize`

- 模型节点默认 DeepSeek（无 Key 回退规则）
- 工具：`validate_task_draft` / `repair_task_draft` / `drop_items` / `export_markdown`（`drop_items` 也用于审核 HITL）
- 有 Key 时 `validate_repair` 为有上限的 LLM 工具循环；无 Key 为规则修复
- Checkpointer 与 `MemoryStore` 共存：前者管图游标，后者管 ThreadSnapshot / 幂等
- 负责人 ERROR 不自动修复；3 次分配后仍非法则带 ERROR 进 review

## 启动

```powershell
uv sync --extra dev
copy .env.example .env
uv run uvicorn app.main:app --reload --port 8001
```

## 测试

```powershell
uv run pytest
uv run ruff check app tests evals
uv run python -m evals.generate_cases
```

评测必须 `LLM_PROVIDER=none`。Mock LLM 用例见 `tests/test_workflow.py`（respx）。真 API 报告：`uv run python -m evals.report`（不改 cassette）。
