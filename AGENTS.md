# SpecOps Agent 项目规范

适用于整个仓库。开始任务时先读本文，再读相关源码、测试和 ADR；用户当次明确要求优先。本文保存长期约定，不记录临时任务进度或未经验证的完成结论。

## 1. 沟通与操作

- 默认用中文回复。目标或动机不清晰时先讨论；目标明确但方案不佳时，直接说明原因并建议更好的办法。
- 先理解实际调用链再修改，解决根因；每个设计决策都要能解释为什么。优先复用现有实现，不为当前项目不可能出现的情况增加抽象或兜底。
- 下载、安装任何软件、工具、依赖包或可执行文件前，必须说明用途并取得用户明确确认。除 skill 外，安装位置必须在 `E:\codex\_xz`；不得直接照抄 README 的安装命令到默认目录。已有环境可复用，不擅自迁移。
- 每次修改文件前，重新读取目标文件的最新内容；新建前检查是否已存在。
- 开始编辑前查看 `git status --short`，保留用户已有的修改和未跟踪文件；不擅自重置、覆盖或提交。
- 删除重要或敏感文件前必须征得同意。任务结束清理本次生成的垃圾、测试临时文件和无关文件，不清理用户原有文件。
- 判断目录为空前，必须再用 `dir` 命令验证。遇到权限拒绝时不能把目录当空目录，也不擅自改权限。
- 调用 PowerShell 命令时必须显式启动以下形式，并使用 UTF-8；读取文本使用 `Get-Content -Encoding UTF8`，写入文本同样指定 UTF-8。

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; <具体命令>"
```

## 2. 项目定位与代码入口

把中文会议对话转成带原文证据、主责和次责的任务清单，经过澄清、校验和人工审核后导出 Markdown / JSON。

| 目录 / 文件 | 职责 |
| --- | --- |
| `backend/app/api/` | FastAPI 请求校验、聊天流、人工续跑、线程读取与导出 |
| `backend/app/agent/graph.py` | LangGraph 节点和路由拓扑 |
| `backend/app/agent/nodes.py` | 节点行为、澄清、修复、负责人建议与人工审核 |
| `backend/app/agent/state.py`、`workflow.py` | 图状态、快照转换、执行、恢复与 SSE |
| `backend/app/domain/` | Pydantic 领域模型、分块、规则抽取、校验和删除意图 |
| `backend/app/llm/` | DeepSeek 结构化调用、共享 HTTP 客户端、并发与重试 |
| `backend/app/memory/` | SQLite 业务快照、请求幂等、线程占用和 checkpoint |
| `backend/app/obs.py` | 请求上下文、JSON 日志、模型调用额度与用量累加 |
| `backend/tests/`、`backend/evals/` | 回归测试、冻结 golden、hard 集、cassette 回放和真实调用报告 |
| `frontend/src/`、`frontend/e2e/` | Vue 3 / TypeScript 界面、会话恢复、Vitest 与 Playwright |
| `docs/adr/` | 关键设计原因与取舍；使用前核对当前实现 |

后端 Python 3.11+，CI 使用 Python 3.12；前端 Node.js 22+。依赖由 `backend/pyproject.toml` / `uv.lock` 和 `frontend/package.json` / `package-lock.json` 管理。不要顺手升级依赖或重写锁文件。

## 3. 业务不可破坏的约定

1. **证据可追溯。** `evidence_kind=source` 时，`evidence_quote` 必须非空且为原始 `source_text` 的连续子串，不能改写成更顺的句子后冒充引用。推断使用 `inference` 并保留 `inference_reason`；保留 `acceptance_origin` 对会议、系统和人工验收标准的区分。
2. **原文与修订分开。** 人工修订意见保存在 `revision_notes` 等对应字段，不拼进原文伪造证据；全文重跑也不能丢失原始证据边界。
3. **建议不等于写入。** `propose_owners` 只生成 `owner_suggestions`；建议只有经人工分配或审核提交后才能进入主责 / 次责。`confidence` 只作展示，不作自动选人门槛。模型抽取原文明确的负责人不受此限制。
4. **缺负责人交给人。** 一条任务只有一位主责，可有多位次责。缺失、非法或指代未消解的负责人进入人工分配，不猜人，不填“待指定”充数；规则和模型修复均跳过负责人类错误。
5. **人工分配有上限。** 当前最多分配 3 次，仍有负责人 ERROR 时携问题进入 review，由人显式批准；不要在 finalize 重新拦截并制造死循环。不能把“可人工批准”解释为可自动忽略校验问题。
6. **离线路径可用。** `LLM_PROVIDER=none` 或没有 Key 时保留确定性规则路径；hybrid 抽取失败时保留规则底稿。普通测试和 CI 不依赖真实模型或外网 Key。
7. **审核动作明确。** 批准、单项修订、删除、全文重跑各走自己的路由。`approve=false` 不代表拒绝；当前没有独立的 `rejected` 终态，不能声称已实现。新增拒绝能力时须一起设计请求、状态、路由和结束行为。
8. **交付与恢复数据分开。** JSON 导出使用 `ThreadSnapshot.export_dict()` 剥离 `owner_suggestions`；供界面恢复的线程快照保留建议。按任务 `id` 修改或删除，不用界面排序后的下标替代任务标识。

## 4. 图执行、恢复与幂等

主流程如下，条件和回路以 `graph.py` 与 `nodes.py` 为准：

```text
intake → clarify? → decompose → validate_repair
       → propose_owners? → assign_owners? → review → finalize
```

- SQLite 业务快照保存用户可见清单、审核状态、幂等信息和用量；LangGraph checkpointer 保存图执行状态，两者不能互相替代。持久化启动路径使用 SQLite checkpointer，`MemorySaver` 只用于不要求跨重启恢复的测试。
- 区分 `thread_id`（业务会话）、`run_id`（本次执行）、`request_id`（请求幂等）与 `graph_ns`（checkpoint 键）。`configurable.thread_id` 使用 `graph_ns`，同一图执行的 HITL 续跑必须保持它不变。
- 恢复使用 `Command(resume=...)`。`interrupt()` 恢复时节点会重新执行，不是从原 Python 栈继续；中断前的外部调用或写入必须避免被重复执行。
- 续跑必须匹配当前 `interrupt_id` 和中断类型；旧页面、旧中断或错误类型的提交不能推进当前图。
- 使用 `MemoryStore.claim_operation()` 的事务性认领逻辑。同一 `request_id`、同一操作可回放；相同 ID 对应不同请求内容须冲突；线程忙碌时不能并发开启另一趟执行。不要另加只在单进程有效的幂等字典。
- 保留 checkpoint 与快照对齐逻辑；刷新后从服务端恢复，不用前端旧状态推断图已完成。
- `pending_events`、`eval_pre_enrich_stakeholders`、`eval_scored_items` 使用 `_replace`，含义是覆盖而非追加。还原状态时清空本次未提交的澄清 / 分配 / 审核动作，防止上次动作残留。

## 5. 调用预算、SSE 与界面

- 模型配置集中在 `backend/app/config.py`，环境变量名称与 `.env.example` 保持一致。不要把 Key 放进前端、源码、日志或测试数据。
- 保留工具循环的三重上限：轮次、连续失败和错误数停滞；当前默认分别为 4 / 2 / 2，退出后仍做最终校验。无 Key 的规则修复最多 2 次，不修负责人。
- 传输重试只覆盖 429、5xx 和传输错误；超时不重试，退避睡眠有总预算。结构化解析另有一轮 schema 重试，计算调用上限时必须一起计入。
- 实际 HTTP 请求受 `LLM_MAX_CONCURRENCY` 和每执行段的 `LLM_MAX_CALLS_PER_RUN` 约束；额度包含传输重试和 schema 重试。复用进程共享客户端，退避不占 HTTP 并发槽。
- SSE 的 `progress.status` 只用 `running | done | skipped | waiting`；`interrupt.type` 为 `clarify | assign_owners | review`。改事件协议时同步后端、前端解析、进度条和测试。
- 节点 `done` 和 SSE `done` 都不等于任务获批完成；中断或错误也会结束一段流。界面根据快照状态及当前 interrupt 展示阶段；正常返回 `result` 前完成对应快照持久化。
- 前端使用 `sessionStorage` 保存当前标签页的线程标识，刷新后拉服务端快照；未提交的负责人建议不能因刷新变成正式负责人。一次流没有最终结果时应补查快照，迟到的旧响应不能覆盖新状态。
- Markdown 预览保留 `DOMPurify.sanitize()`，不得把未经清理的模型输出直接交给 `v-html`。
- `token_in` / `token_out` 记录已取得响应的调用用量；`latency_ms` 是成功调用耗时之和，`wall_ms` 是本执行段墙钟，不含人工停留。不要混用，也不要把未统计到的失败调用说成零成本。

## 6. 验证与评测

只使用已经安装并确认可用的环境。下面是验证命令，不是安装授权；缺工具或浏览器时先报告，再按第 1 节取得安装确认。调用这些命令时仍须遵守 PowerShell 启动和 UTF-8 要求。

后端在 `backend/` 执行，禁用自动同步和 Python 下载；预先将 `LLM_PROVIDER` 设为 `none`：

```text
uv run --no-sync --no-python-downloads ruff check app tests evals
uv run --no-sync --no-python-downloads pytest -q
```

前端在 `frontend/` 执行：

```text
npm test
npm run build
```

涉及跨端流程、刷新恢复或人工审核时，在已有后端环境和 Playwright 浏览器可用的前提下执行：

```text
npm run test:e2e
```

- 根据变更选择有意义的检查。纯文档修改核对内容、路径和差异即可；修改逻辑时补充能暴露该问题的回归用例，不为简单文案堆测试。
- 普通后端测试使用 mock / cassette，真实 API 的可用性、语义效果、成本需单独实测，不能用离线通过代替。
- 冻结 golden 衡量规则回归：召回 ≥ 0.9，主责 exact ≥ 0.8，次责集合相等 ≥ 0.8，HITL 一致率 100%；这些门槛不是模型准确率。
- hard 集、cassette、`hard_expected.json` 和真实 API 报告各有用途；不能为了变绿而降低冻结下限、重录覆盖失败或把未知结果写成通过。
- `test_eval_golden.py` 会调用生成器写 golden 文件；跑后检查差异，保留用户已有变更，不盲目覆盖或重置。改 prompt / 抽取行为时核对对应评测，真实重录涉及外部调用和费用，应明确范围后执行。
- 最终报告说明改了什么、验证了什么、哪些未验证及原因。保留真实模型调用与离线回归的界限，不复用旧测试报告冒充本次结果。

## 7. 安全、文档与交付

- 不提交 `.env`、API Key、凭据、SQLite 数据库及旁文件、日志、缓存、构建产物或本地 IDE 配置；示例配置只留空值或安全占位符。无需读真实 `.env` 即可完成的任务不要读取它。
- 本地快照和 cassette 可能包含会议原文，不能随意公开或上传。新增第三方观测时须确认数据范围与脱敏，不能改变业务快照和评测结果。
- 修改 API 字段、状态枚举、图路由、SSE、配置或导出行为时，同步对应的前后端契约、测试和文档；重要设计取舍记录到 `docs/adr/`。
- `README.md` 提供运行入口，`docs/adr/0001` 至 `0006` 说明核心取舍，`docs/测试报告.md` 记录历史验证。文档与源码不符时应指出并核对，不能仅凭 ADR 文件存在就断言功能已实现。
- README 中的 Production Hardening Roadmap 是未实现清单。鉴权、多租户隔离、入口限流、外部工单同步等没有实际实现和证据时，不对外宣称已具备或已生产验收。
- 完成前检查本次差异，只交付任务所需文件，不顺手重构、不改无关内容、不留下临时产物。
