# 观测是旁路，默认关闭

## 背景

模型调用、节点耗时和 HITL 停留，现在只能靠 SQLite 快照和一行一条 JSON 的日志。要回答「哪一步慢、哪次抽取跑偏、换 prompt 之后差在哪」，得翻日志拼。

但快照里的 `token_in` / `token_out` / `latency_ms` 是黄金集门槛的依据，冻结评测又强制 `LLM_PROVIDER=none`。观测不能顺手改这些数字，也不能在测试里发出任何请求（`respx` 拦截全部出站）。

## 决策

接 Langfuse（Python SDK v4，OpenTelemetry）做纯旁路：

- 埋点只有三处：请求级 span（`run_chat` / `resume` 各一条）、5 个非 HITL 节点的 span、`LLMClient.post_completions` 里的 generation。
- 一次 HTTP 请求一条 trace，`session_id = thread_id` 把一次会话的多次 HITL 串起来。
- `clarify` / `assign_owners` / `review` 不包 span：它们被 `interrupt()` 中断后 resume 会重跑，包了会留下重复且被误标 error 的半截 span。
- 默认关闭。无 Key 或 `LANGFUSE_TRACING_ENABLED=false` 时全部 no-op；`run_chat` / `resume` 是 async generator，不用 `@observe` 装饰，改手动 observation 加显式 `end()`。
- 不上传 `source_text` 与 `evidence_quote`，只传结构、prompt、usage、耗时和成本。
- 不用 `langfuse.langchain.CallbackHandler`：它把 resume 拼回同一条 trace 的前提是「resume 时没有 active OTel span」，与请求级 span 冲突，且这条前提是代码级证据、不是文档承诺。

## 取舍

人工等待前后是两条 trace，不是一条；跨 HITL 的完整链路靠 session 视图看，不能指望单条 trace 走到底。换来的是不依赖 langchain-core 的 callback 内部实现，也不和请求级 span 抢 OTel 上下文。

快照仍是 token 和延迟的唯一真相。Langfuse 里的数字只用于排查，不写回快照、不进断言、不进 `evals/report.md` 的下限。

## 后果

无 Key 时本地全流程和黄金集照跑，行为与接观测之前逐字一致；测试环境里连客户端都不构造。

原文不出网，意味着排查「模型为什么抽错」仍要回到本地快照和 cassette，不能只靠 Langfuse 界面。想看真实 prompt 就要显式打开 `LANGFUSE_CAPTURE_SOURCE_TEXT`，并先补 `mask_otel_spans` 脱敏。
