# 阻断型校验与人工放行

## 背景

审核面板原来只有一条判断：`ValidationResult.has_blocking_errors` 为真就提示「仍有问题」，而「一键批准并完成」对所有 ERROR 一视同仁——只要能过 review，就能进 finalize 导出。于是 `MISSING_EVIDENCE_QUOTE`、`INVALID_EVIDENCE_QUOTE` 这类直接破坏「证据可追溯」的 ERROR，和 `MISSING_OWNER` 这种「该交给人工分配、分配满 3 次后允许人显式接手」的 ERROR 走了同一条出口：人工点一下批准，坏证据就进了交付物。

同时另一头太松：分块抽取部分失败时，`decompose` 只往 `validation.issues` 追加一条 advisory 就结束了。批准、单项修订、负责人分配、`validate_repair` 和全文重跑都会重新跑校验并重新构造 `ValidationResult`，那条 advisory 随第一次重算一起消失——审核面板看起来一切正常，导出也看不出「这次模型只抽到一半」。

两头合起来是同一个问题：**「能不能导出」缺少一条显式判定，而「本次结果完整不完整」缺少一个权威来源。**

## 决策

> 以下写的是**决定要怎么做**，不等于已经做完；每一条的实际落地情况见文末「落地状态」。

### 1. ERROR 三分类，只有阻断型需要放行

按 `backend/app/domain/validation.py` 现有的集合拆成三类：

| 分类 | 集合 | 走法 |
| --- | --- | --- |
| 负责人类 | `OWNER_ERROR_CODES` = `MISSING_OWNER` / `INVALID_PRIMARY` / `INVALID_SECONDARY` / `PRIMARY_IN_SECONDARY` / `UNRESOLVED_REFERENCE` | 不阻断导出。进 `propose_owners → assign_owners`，最多 3 次后携错进 review，由人显式批准（既有约定） |
| 可批准类 | `APPROVABLE_ERROR_CODES = frozenset({"EMPTY_TASK_LIST"})` | 不阻断普通批准。空清单是合法终态 |
| 其余全部 | 未列名的一切 ERROR | **阻断**。普通 `approve=true` 不放行，必须逐条修，或填理由后显式 override |

实现入口只有两个：`blocking_codes(validation)` 返回去重保序的阻断码列表，`has_non_owner_errors(validation)` 就是 `bool(blocking_codes(...))`。不要在各节点里各自再写一遍 `any(...)` 判断，也不要在前端重算——判定归后端，前端只读 `blocking_errors`。

**白名单豁免，不是黑名单列举。** 新增 ERROR 码默认属于阻断型，忘了加进集合的最坏后果是「多要一次人工确认」，而不是「静默放过一个真问题」。反过来做黑名单，每加一个错误码就得记得补一条，补漏的表现正是本轮要修的这个 bug。

### 2. override 契约

`POST /api/v1/runs/{run_id}/resume` 的审核批准请求新增两个可选字段：

- `override_reason: str | None = None`。`None` 表示未提供；非 `None` 但 trim 后为空 → **422**（空白理由等于没填，不能当成"人是确认过的"）；非空 → trim 后落库。
- `operator_claim: str = ""`。操作者**自述**，未认证。它可以写「值班同学 张三」，但系统没有身份凭证，导出和快照里都要按自述标注。

校验规则：`approve=true` 且存在阻断型 ERROR 时 `override_reason` 必填；`approve=false` 或没有阻断型错误时传了也照常接受（不强制、不报错，避免前端状态切换时反复 422）。

快照侧记录在 `ReviewInfo`：`override_reason`、`override_codes`（本次放行的阻断码，去重保序）、`override_at`（ISO 时间）、`operator_claim`。`override_codes` 记的是**放行时那批码**，不是事后重算的当前码——否则单条修订把某个错误修掉了，历史记录会自己变干净。

review 的 interrupt payload 同步新增 `blocking_errors`，每项 `{code, message, severity, item_id, field}`，为空数组表示普通批准即可放行。`unresolved_errors` / `advisory_issues` / `items` 保留原样：`unresolved_errors` 仍然是**全部** ERROR（含负责人错误），`blocking_errors` 只挑出必须放行的那批。两者不是子集替代关系——前端存在 `unresolved_errors` 但没有 `blocking_errors` 时，仍然可以走普通批准。

### 3. Markdown 导出加「人工放行」，但不清洗「未解决问题」

有 `override_reason` 时，导出的 Markdown 在「未解决问题」**之后**追加一节（先列问题、再记放行条件，读的人先看到问题本身）：

```text
## 人工放行

- 放行人（自述，未认证）：<operator_claim，空则「（未填写）」>
- 放行时间：<override_at，无则「（未记录）」>
- 放行理由：<override_reason>
- 放行问题码：<override_codes，空则「（未记录）」>
```

「未解决问题」一节**保持原样输出全部 issues**，不能因为人工放行就把它们从交付物里删掉。放行改变的是「能否导出」，不是「问题是否存在」。下游拿到 Markdown 的人有权知道这份清单是在什么条件下被批准的。

### 4. 部分抽取：`ThreadSnapshot.extraction` 是唯一权威来源

`ExtractionInfo`（`chunks_total` / `chunks_ok` / `chunks_empty` / `chunks_failed` / `partial` / `degraded` / `detail`）挂在快照上，记本次分块抽取的实际覆盖。

- `partial=true`：有块失败但有块成功，结果不完整。
- `degraded=true`：所有块都失败，只剩规则底稿。

advisory 由 `with_extraction_issues(validation, extraction)` 从 `extraction` **派生**，不是从上游传下来的存量：每次重新校验后都调用它，按 `issue.code` 去重后注入 `PARTIAL_EXTRACTION` / `RULE_FALLBACK_ONLY`。**禁止只在 `decompose` 后 append 一次**，那必然会在下一次重算时丢掉。

同理，前端不要自己判断「有没有部分失败」，读快照的 `extraction` 或 payload 里的 `extraction`；`partial || degraded` 为真时，审核区和完成页都必须明示「本次有 N 个分块抽取失败，结果可能不完整」/「仅规则结果」。

## 取舍

**为什么不把阻断判断留给前端？** 前端能拿到 `unresolved_errors` 自己过滤，但那样「哪些码阻断」就有了两份实现，且可以绕过 API 直接发请求。判定必须在后端生效，前端只是它的投影。

**为什么不把 `EMPTY_TASK_LIST` 纳入阻断？** 人工把任务删光是一个合法的审核结论——「本次会议没有可执行任务」，导出里已经有专门的「本次无任务」章节。把它算作阻断，等于逼用户为一个他刚刚亲手做出的决定再填一次理由，同时还破坏既有 `test_approve_empty_list_exports` 的语义。它是 ERROR（因为它确实不是正常路径的产物，值得记录），但不是需要修复的问题。

**为什么 override 只要理由，不要二次确认？** 再点一次「我确定」不产生任何新信息，只会训练用户盲点。真正有约束力的是**记录**：理由、码、时间、自述者都进快照并进导出，事后可追。代价是这不能防住故意乱填的人。

**为什么 operator_claim 不做成认证字段？** 当前没有鉴权（见 README 的 Production Hardening Roadmap）。字段名和占位文案都写明「未认证自述」，是为了在还没有身份体系的时候，至少不制造「这条记录能追到人」的错觉。

## 后果

- 未知 ERROR 默认阻断：新增校验规则时不需要同步改前端，也不会被静默放行；代价是新规则上线初期会多几次 override 填写。
- 人工放行有痕迹但不可强约束：快照和导出能说明「在什么条件下批准」，不能证明批准人是谁。
- 部分抽取告警变成幂等派生：任何重算路径都不会丢掉 `PARTIAL_EXTRACTION` / `RULE_FALLBACK_ONLY`，但代价是 `ValidationResult.issues` 里混入了非校验产出的条目，读 `issues` 的代码不能假设它们都来自 `validate_task_draft`。
- 仍然**没有独立的 `rejected` 终态**。`approve=false` 表示「这次不批准、按修订继续」，不表示拒绝完成。引入拒绝语义要一起设计请求、状态、路由和结束行为，不能靠 override 顺带实现。

## 落地状态

本文是设计约定，不是完成证明；以源码为准（与 AGENTS.md 第 7 节的约定一致）。最后一次核对时间：2026-11 审查整改。

> 定位方式：源码在整改期间持续改动，行号会漂移，因此下文按**文件 + 符号名**定位（函数名、类名、集合名、模板里的 `id` 与 class），核对时在对应文件中检索符号即可确认。

**后端已落地：**

- 决策 1 的三分类：`OWNER_ERROR_CODES`、`APPROVABLE_ERROR_CODES = frozenset({"EMPTY_TASK_LIST"})`、`UNREPAIRABLE_CODES`、`blocking_codes()`、`has_non_owner_errors()` 均在 `backend/app/domain/validation.py`；`blocking_codes()` 是白名单豁免实现（未列名的一律阻断）。
- 决策 2 的契约：`ResumeRequest.override_reason: str | None = None` 与 `operator_claim: str = ""`（`backend/app/api/schemas.py`），`model_validator` 里空白理由 → `ValueError("override_reason must not be blank")`、`override_reason` 与 `approve=false` 同时出现 → `ValueError("override_reason requires approve=true")`，都由 FastAPI 转成 422。
- review 节点的批准拦截：`nodes.py` 的 `review_node` 取 `blocking_codes(validation)`，`blocking and not override_reason` 时 `approved=False`、留在 review 并回「存在必须修复的问题，未完成导出」；有阻断项且给了理由时写 `override_reason` / `override_codes` / `override_at` / `operator_claim`，无阻断项时清空上一次的放行记录。
- `finalize` 前的兜底：`needs_override(state)` + `route_after_finalize(state)`，仍有未放行的阻断码就退回 `review`，不会悄悄 END。
- interrupt payload 已带 `blocking_errors` / `advisory_issues` / `extraction`（`nodes.py` review_node 的 interrupt 构造处）。
- 决策 3：Markdown 导出在「未解决问题」之后追加「## 人工放行」，四行分别是放行人（自述，未认证）、放行时间、放行理由、放行问题码；「## 未解决问题」仍输出全部 issues（`backend/app/domain/extractors.py` 的 Markdown 组装函数，检索 `## 未解决问题` 与 `## 人工放行` 即可定位）。
- 决策 4：`ExtractionInfo` 挂在 `ThreadSnapshot.extraction`；`decompose` 按 `stats["failed"] > 0 and stats["ok"] > 0` 置 `partial`、按 `stats["ok"] == 0 and stats["failed"] > 0` 置 `degraded`；`with_extraction_issues(validation, extraction)`（`validation.py`）按 `issue.code` 去重后派生 advisory。

**前端已落地：**

- 状态与派生：`frontend/src/App.vue` 的 `overrideReason` / `operatorClaim` / `showOverride`，以及 `blockingErrors`、`needOverride`、`extraction`、`extractionPartial`、`extractionWarning`、`extractionDetail` 六个 computed；`Snapshot` 类型声明了 `extraction` 与 `review.override_*` / `operator_claim`。
- 阻断可见与拦截：审核面板渲染阻断错误清单（`.issue-list.issue-blocking` 容器，`role="alert"`）；`needOverride` 为真时「一键批准并完成」按钮禁用（`:disabled="loading || (needOverride && !overrideReason.trim())"`），`requestApprove()` 与另一批准入口都先过 `overrideReady()`，理由为空就展开表单并聚焦 `#override-reason`，不发请求。
- 放行表单与提交：`#override-form` 含必填「放行理由」（`#override-reason`）与选填「自述操作人（未认证自述，仅作记录）」（`#operator-claim`）；`resume()` 只在 `approve` 时把 trim 后的 `override_reason` / `operator_claim` 塞进请求体，提交成功后清空 `overrideReason` / `operatorClaim` / `showOverride`，避免下一次批准复用旧理由。
- 部分抽取提示：审核区与 done 页都渲染 `extractionWarning` 与 `extractionDetail`，`watch(needOverride)` 在出现阻断项时自动摊开理由表单。

> 前端代码仍在迭代，具体行号会随模板增删漂移，因此本节按符号名定位（`App.vue` 中的函数名、computed 名、元素 `id` 与 class），不写死行号。核对方式：在 `frontend/src/App.vue` 中检索上述符号即可确认接线位置。

**未落地 / 缺口：**

- **没有独立的 `rejected` 终态**（见「后果」最后一节）。`approve=false` 只是「不批准」，会停在审核并带着问题返回，系统不会把这次审核记成拒绝，也没有拒绝原因字段。本文不构成该能力的实现声明。
- **放行流程的异常分支仍没有 e2e**：正常放行与「无阻断项直接批准」两条主路径已覆盖（见下节「已补上」），但放行后刷新恢复、过期 `REQUEST_EXPIRED` 回放、`interrupt_id` 不匹配这些异常分支还没有 e2e。
- 放行理由的**审计强度有限**：`operator_claim` 是自述字段，没有认证、没有不可抵赖签名，导出里明确标「未认证」。多租户或合规场景需要的「谁在什么身份下放的」不在当前能力内。

**已补上（此前列为缺口，现已过期）：**

- **放行流程有两条互补的跨端用例**。`frontend/e2e/override-approval.spec.ts` 两个 `test`（「阻断错误必须填理由才能放行，放行记录进入完成的 Markdown」「没有阻断错误时不出现放行表单，普通批准直接放行」）用 `page.route` 构造快照，快速验证 UI 契约：阻断时按钮禁用、理由必填、请求体带 `override_reason` / `operator_claim`、完成页展示放行记录；但它**不证明后端真的会阻断**。后端链路由 `backend/tests/test_override_integration.py::test_real_backend_override_flow` 覆盖：真实 FastAPI app + 独立测试 SQLite + 真实图/工作流，用 `graph.aupdate_state` 这个测试专用入口注入非法证据（**不给生产 API 增加随意改快照的公开入口**），真实走 `/chat/stream` → 普通批准被 `review_node` 重算校验阻断 → 换新 `interrupt_id` / `request_id` 带理由二次 `/resume` 完成 → `GET /threads/{id}` 核对 `review.override_*` 已写入业务快照 → `GET /threads/{id}/export?format=md` 核对「## 人工放行」由后端真实生成。
- 部分抽取告警的幂等派生**已有回归用例**：`backend/tests/test_partial_extraction.py` 三个用例 `test_partial_extraction_is_recorded`、`test_partial_warning_survives_item_patch`、`test_partial_warning_survives_approval`，覆盖「记录」「单项修订后仍保留」「批准导出后仍保留」，用假 LLM 客户端 + 替换 `_llm_decompose` 固定 `{"ok": 2, "empty": 0, "failed": 1}`，不依赖 HTTP 桩。
- `EMPTY_TASK_LIST` 的可批准判定已有单测（`backend/tests/test_approval_override.py` 的 `test_blocking_codes_excludes_owner_and_empty_list` 断言它属于 ERROR 但 `blocking_codes` 为空），加上既有 `test_approve_empty_list_exports`（`backend/tests/test_workflow.py`）的完整流程，这条现在有覆盖。
