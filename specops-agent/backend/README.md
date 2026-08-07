# SpecOps Agent — Backend

LangGraph 真实运行时 + FastAPI SSE 薄壳。详见根目录 [README.md](../README.md) 与 [docs/INTERVIEW.md](../docs/INTERVIEW.md)。

## 拓扑

`intake → clarify? → decompose → retrieve_playbooks → validate_repair → review? → finalize`

- 模型节点默认 DeepSeek（无 Key 回退规则）
- 工具：`search_playbooks` / `validate_task_draft` / `repair_task_draft` / `export_markdown`
- Checkpointer 与 `MemoryStore` 共存：前者管图游标，后者管 ThreadSnapshot / 幂等

## 启动

```powershell
pip install -e ".[agent,dev]"
copy .env.example .env
uvicorn app.main:app --reload --port 8001
```

## 测试

```powershell
pytest
ruff check app tests
```

Mock LLM 用例见 `tests/test_workflow.py`（respx）。
