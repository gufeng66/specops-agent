# Langfuse 观测实施说明

配套决策：[ADR 0007 观测是旁路，默认关闭](adr/0007-langfuse-sidecar-tracing.md)。

已定前提（本次确认）：

- **路线 B**：纯手写观测，不用 `langfuse.langchain.CallbackHandler`。
- **原文不出网**：不传 `source_text` / `evidence_quote`，只传结构、prompt、usage、耗时、成本。
- **默认关闭**：无 Key 或未显式开启时全链路 no-op，行为与接观测之前逐字一致。

## 1. 目标链路

```text
一次 HTTP 请求 = 一条 trace（run_chat 或 resume 各一条）
  └─ session_id = thread_id            ← 把一次会话的多次 HITL 串起来
     ├─ span  specops.run              手动创建，显式 end()
     ├─ span  node=intake | decompose | validate_repair | propose_owners | finalize
     └─ span  generation=deepseek      收口在 LLMClient.post_completions
```

`clarify` / `assign_owners` / `review` **不包 span**。它们被 `interrupt()` 中断，resume 时节点函数从头重跑，包了会出现重复 span 且被误标 error。

## 2. 依赖与配置

```toml
# backend/pyproject.toml
dependencies = [
    # ...
    "langfuse>=4.8.1,<5",
]
```

- v4 是 OpenTelemetry 重写版（2026-03），`requires_python >=3.10,<4.0`，Python 3.11 / CI 的 3.12 都满足。
- 下限取 4.8.1：v4 的 Scores API 客户端在更早的 4.x 里没有。
- 直接依赖会带进 `opentelemetry-api/sdk` + `opentelemetry-exporter-otlp-proto-http`。
- Langfuse Cloud 的 `/api/public/ingestion` 从 **2026-11-16** 起只接收 scores，v4 的 OTel 链路是唯一在实时链路上的选择。

```dotenv
# backend/.env.example（新增）
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
LANGFUSE_RELEASE=
LANGFUSE_CAPTURE_SOURCE_TEXT=false
```

- 只设 `LANGFUSE_BASE_URL`，**不要**同时设 `LANGFUSE_HOST`（遗留回退名，两个都设会打架）。
- 真实 `.env` 已被 `.gitignore` 排除；`backend/.env` 目前只有 DeepSeek 8 项，需要自己补上面这几行。

```python
# app/config.py 新增字段
langfuse_enabled: bool = False
langfuse_public_key: str = ""
langfuse_secret_key: str = ""
langfuse_base_url: str = "https://us.cloud.langfuse.com"
langfuse_tracing_environment: str = "development"
langfuse_release: str = ""
langfuse_capture_source_text: bool = False

@property
def tracing_ready(self) -> bool:
    return bool(self.langfuse_enabled and self.langfuse_public_key and self.langfuse_secret_key)
```

```yaml
# docker-compose.yml backend.environment 追加
LANGFUSE_ENABLED: ${LANGFUSE_ENABLED:-false}
LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:-}
LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY:-}
LANGFUSE_BASE_URL: ${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}
LANGFUSE_TRACING_ENVIRONMENT: ${LANGFUSE_TRACING_ENVIRONMENT:-development}
LANGFUSE_CAPTURE_SOURCE_TEXT: "false"
```

```yaml
# .github/workflows/ci.yml backend 步骤 env 追加（双保险）
LANGFUSE_TRACING_ENABLED: "false"
```

## 3. 代码改动清单

### 3.1 新增 `backend/app/tracing.py`

`obs.py` 不动，日志与计数职责保持单一。

```python
_client: Langfuse | None = None

def init_tracing() -> None:
    global _client
    if not settings.tracing_ready:
        return
    _client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,   # 显式传入，env 覆盖不了
        environment=settings.langfuse_tracing_environment,
        release=settings.langfuse_release or PROMPT_VERSION,
        tracing_enabled=True,                  # 显式，不依赖 env 字符串大小写
        timeout=10,                            # SDK 默认只有 5s
        debug=False,
    )
    if not _client.auth_check():
        logger.warning("langfuse auth_check failed; tracing stays degraded")

def shutdown_tracing() -> None:
    if _client is not None:
        _client.shutdown()                     # flush + join 后台线程
```

对外只暴露：`current_observation()`、`start_request_trace(...)`、`end_request_trace(...)`、`node_span(name)`、`generation_span(...)`。全部在 `_client is None` 时返回 `None` 或空上下文。

**父子关系必须用父对象方法**：官方明确，全局 `start_as_current_observation()` 不会成为手动 `start_observation()` 父节点的子节点，必须 `parent.start_observation(...)`。请求级 observation 存进 ContextVar（沿用 `obs.py` 里 `bind_request` 的写法），节点与 generation 从 ContextVar 取父对象。

### 3.2 `backend/app/main.py`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_tracing()                 # 在 SQLite 之前，失败也不影响启动
    ...
    try:
        yield
    finally:
        reset_compiled_graph()
        await close_checkpointer()
        shutdown_tracing()         # SDK 有 atexit，长驻服务仍建议手动调
```

### 3.3 `backend/app/agent/workflow.py`

`run_chat` / `resume` 是 async generator，**不要装饰 `@observe`**（在 async generator 上会产生 0 时长、层级拍平的 observation）。

```python
root = start_request_trace(snapshot, request_id, run_id)   # no-op 时返回 None
try:
    with bind_request(request_id, run_id):
        ...                     # 原有逻辑不动
finally:
    end_request_trace(root)     # 客户端断连走 GeneratorExit 也能兜住
```

metadata 至少带：`request_id`、`run_id`、`thread_id`、`graph_ns`、`prompt_version`。

### 3.4 `backend/app/agent/graph.py`

只包 5 个非 HITL 节点，用 `functools.wraps` 保签名（LangGraph 会看节点函数签名）：

```python
builder.add_node("intake", _traced("intake", intake_node))
# decompose / validate_repair / propose_owners / finalize 同理
builder.add_node("clarify", clarify_node)          # HITL，不包
builder.add_node("assign_owners", assign_owners_node)
builder.add_node("review", review_node)
```

### 3.5 `backend/app/llm/client.py`

`post_completions` 是全部 LLM 调用的唯一收口——`chat_json`、`chat_tools` 以及 `evals/report.py` 的 judge 都过它，天然带 `node`、耗时和 usage。

- 一次逻辑调用一个 generation observation（含重试），`metadata` 记 `attempts`。
- `model=settings.deepseek_model`，`usage_details={"input": prompt_tokens, "output": completion_tokens}`。
- **DeepSeek 不在 Langfuse 内置价目表里**，成本必须显式传 `cost_details`，复用 `evals/report.py` 的 `USD_PER_M_IN` / `USD_PER_M_OUT`。
- `input` 只放 prompt；按已定前提，不放 `source_text` 原文。
- 导出失败不能影响业务：SDK 承诺 flush "never throw"，但仍要在自身代码里兜一层 try/except。

### 3.6 可选：前端回链（P3）

trace id 可用 `Langfuse.create_trace_id(seed=...)` 确定性重算（32 位小写 hex），省掉快照改字段；若要跨 resume 稳定，就在 `ThreadSnapshot` 加 `trace_id`，URL 形如 `{LANGFUSE_BASE_URL}/project/{projectId}/traces/{traceId}`，需要再配一个 `LANGFUSE_PROJECT_ID`。

### 3.7 可选：评测打分（P4）

`evals/report.py` 用 `create_score(name=..., value=..., trace_id=...)` 推 `recall` / `precision` / `semantic_recall`。与 ADR 0006 保持一致：**scores 只进真 API 报告，绝不替代 `hard_expected.json` 的冻结门槛**。更重的做法是 dataset + `run_experiment`。

## 4. 测试

新增 `backend/tests/conftest.py`，autouse 强制关闭（必须在任何 `app.*` import 之前生效）。测试环境**连客户端都不构造**，而不是构造了再关。

新增 `backend/tests/test_tracing.py`：

1. 未配置 → `_client is None`，`request_trace` 是 no-op，快照 `token_in` / `latency_ms` 与改动前逐字一致。
2. 假 Key + respx 拦截 `POST {LANGFUSE_BASE_URL}/api/public/otel` → 断言导出发生，且导出失败时业务返回值不受影响。
3. rules 路径跑完整 workflow，断言 span 存在、`interrupt` 前后 session 一致。

## 5. 动代码之前先做的 4 个验证

这几处官方文档自己都不一致或未承诺，猜错代价比验证大（30–60 分钟）：

1. `auth_check()` 返回 true，且 `tracing_enabled=False` 时**零出站**。
2. `usage_details` 的 key 写法：官方两处例子分别用 `{"input_tokens",...}` 和 `{"prompt_tokens",...}`，**未统一**；跑一次真调用，在 UI 里确认成本/用量是否被识别。
3. 手动 observation 模式下 trace 级属性的确切调用方式（`propagate_attributes(...)` 与根 observation 的 trace IO 方法），照 python.reference 定稿。
4. resume 之后是新 trace 还是被拼接——确认走路线 B 的预期形态（应是新 trace，靠 session 分组）。

命令：

```powershell
cd backend
uv run python -c "from langfuse import get_client; print(get_client().auth_check())"
```

## 6. 排期

| 阶段 | 内容 | 量级 |
| --- | --- | --- |
| 验证 | 上面 4 项 + 一条真 trace 看层级 | 0.5 小时 |
| P0 | 依赖、配置、`tracing.py`、lifespan、compose/CI/env.example | 0.5 天 |
| P1 | 请求级 span、5 个节点 span、generation 收口、conftest + 3 条测试 | 1 天 |
| P3 | 快照 `trace_id` + 前端外链 | 0.5 天 |
| P4 | scores 进报告、dataset/experiment、`mask_otel_spans`、采样与告警 | 1 天 |

## 7. 风险与对策

| 风险 | 对策 |
| --- | --- |
| async generator 上 `@observe` 拍平 | 绝不装饰 `run_chat` / `resume`，改手动 observation + 显式 end |
| `respx` 拦截所有出站，测试撞红 | 默认关闭 + conftest 不构造客户端 + CI env |
| `interrupt()` 重跑 → span 重复/误标 error | 3 个 HITL 节点不包 span |
| v4 默认只导出 `langfuse-sdk` scope / `gen_ai.*` | 手写 span 会被导出；将来引入第三方 OTel 时用 `should_export_span`（`blocked_instrumentation_scopes` 已弃用） |
| 双写指标不一致 | 快照是唯一真相，Langfuse 纯旁路 |
| 原文/姓名出网 | 默认不传原文；要开就配 `mask_otel_spans`（legacy `mask` 在 v4 上曾完全不生效） |
| 成本显示 0 | 显式传 `cost_details` |
| otel 三件套体积 | 可退化为 optional extra + 惰性 import，代价是 CI 测不到观测代码 |

## 8. 回归门

```powershell
cd backend
uv run ruff check app tests evals
uv run pytest
uv run pytest tests/test_eval_golden.py -q      # 需 LLM_PROVIDER=none
cd ..\frontend; npm test; npm run build
```

## 9. 参考

- [PyPI langfuse 4.17.0](https://pypi.org/project/langfuse/)：v4 / OTel、Python 版本、依赖清单
- [SDK Overview](https://langfuse.com/docs/observability/sdk/overview)：US 区 host、ingestion 2026-11-16 变更
- [Python v3 → v4](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4)：`start_as_current_observation` / `propagate_attributes`、默认 span 过滤
- [Instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation)：context manager / `@observe` / 手动 observation 的父子规则
- [Best Practices](https://langfuse.com/docs/observability/best-practices)：一次会话一个 session、一轮一条 trace
- [Troubleshooting & FAQ](https://langfuse.com/docs/observability/sdk/troubleshooting-and-faq)：`tracing_enabled`、`sample_rate`、async 上下文
- [PR #1632](https://github.com/langfuse/langfuse-python/pull/1632)、[issue #10962](https://github.com/langfuse/langfuse/issues/10962)：resume 拼接及其前提
- [issue #7749](https://github.com/langfuse/langfuse/issues/7749)：async generator 上 `@observe` 的层级问题
- [issue #12587](https://github.com/langfuse/langfuse/issues/12587)：legacy `mask` 在 OTLP 导出上不生效
