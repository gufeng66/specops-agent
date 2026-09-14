# SpecOps Agent — Backend

LangGraph 真实运行时 + FastAPI SSE 薄壳。详见根目录 [README.md](../README.md) 与 [docs/INTERVIEW.md](../docs/INTERVIEW.md)。

## 拓扑

`intake → clarify? → decompose → validate_repair → propose_owners? → assign_owners? → review? → finalize`

- 模型节点默认 DeepSeek（无 Key 回退规则）
- 工具：`validate_task_draft` / `repair_task_draft` / `drop_items` / `export_markdown`（`drop_items` 也用于审核 HITL）
- 有 Key 时 `validate_repair` 为有上限的 LLM 工具循环；无 Key 为规则修复
- Checkpointer 与 `MemoryStore` 共存：前者管图游标，后者管 ThreadSnapshot / 幂等
- 负责人 ERROR 不自动修复；3 次分配后仍非法则带 ERROR 进 review

## 启动

```powershell
pip install -e ".[agent,dev]"
copy .env.example .env
uvicorn app.main:app --reload --port 8001
```

## 测试

```powershell
pytest
ruff check app tests evals
python -m evals.generate_cases
```

评测必须 `LLM_PROVIDER=none`。Mock LLM 用例见 `tests/test_workflow.py`（respx）。
