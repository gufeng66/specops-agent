# SpecOps Agent — 面试说明

决策记录：[证据子串](adr/0001-evidence-substring.md)、[建议不等于写入](adr/0002-suggestions-are-not-writes.md)、[工具循环刹车](adr/0003-tool-loop-brakes.md)、[无 Key 走规则](adr/0004-rules-without-key.md)、[checkpoint 与幂等](adr/0005-checkpoint-and-idempotency.md)、[黄金集不是模型效果](adr/0006-golden-is-not-model-quality.md)、[观测是旁路默认关闭](adr/0007-langfuse-sidecar-tracing.md)。

## 项目一句话

把中文会议对话变成「带原文证据的任务清单」，并拆出每项任务的**主责 / 次责**。原文确认不了负责人时必须人工分配，规则路径禁止填写「待指定」。

## 为什么这样设计

- **主次责**：多人协作是会议里的常态。「周婷统筹，孙磊、邓凯配合」若只保留第一个人名会丢责任关系。
- **HITL 而不是「待指定」**：空主责、主责不在干系人、次责非法、主次冲突、**未消解的他/她**都是 ERROR，进入 `propose_owners`（模型建议）再 `assign_owners`。建议 ≠ 写入：`primary_owner` 在人点「提交分配」前保持为空。瞎填占位符会让导出看起来完整、实际不可执行。
- **建议确认制**：有 Key 时 `propose_owners` 在主责已合法时仍会跑，为有协作信号的任务提次责候选；缺主责时仍提主责候选。`confidence` 只映射成低/中/高供参考，不按分数自动选人。无 Key 或模型失败时建议为空。JSON 导出剥离 `owner_suggestions`。
- **3 次后进审核**：避免分配死循环。带着 ERROR 进 `review`，由人显式批准；`finalize` 不再二次拦截——人工已经拍板。
- **没有检索 / RAG**：Playbook 关键词检索曾是展示节点，不进入拆解或校验。已删除，避免被问成假 RAG。
- **规则回退**：无 API Key 时系统仍能跑完；CI 和冻结评测强制 `llm_client.enabled is False`。
- **校验工具循环**：有 Key 时 `validate_repair` 由模型决定调用 `validate_task_draft` / `repair_task_draft` / `drop_items`，往返上限 4，连续失败 2 或 error 数不下降（stall）即停；循环后再做一次最终 validate。无 Key 仍走规则修复 ≤ 2。LLM 工具循环里的 `drop_items` 仍在；**审核 HITL 也能 drop**（按钮 UUID 或「第 N 条不是任务，删掉」），规则假阳性不再只能靠模型删。
- **长文本不是 RAG**：规则扫全文作底稿；LLM 按块只抽 items（句边界切分 + overlap，并发 gather）；去重用归一化标题 + quote 起始偏移窗口（<300 字）+ owner。brief 单独一次（规则全文 + 放宽/首尾采样的 LLM），再用任务负责人补干系人，不做块摘要并集。分块换更高成本和延迟，换取不引 RAG 的简单性；并发把延迟压在单块超时量级。不做块中途 SSE：`astream(updates)` 仍只在节点结束后 flush。

## 三种人工介入

| 类型 | 触发 | 恢复 |
| --- | --- | --- |
| `clarify` | 会议目标为空 | `/chat/stream` + `clarify_response` |
| `assign_owners` | 任一 owner 校验 ERROR（含 `UNRESOLVED_REFERENCE`）；面板可展示模型建议 | `/runs/{id}/resume` + `assignments` |
| `review` | 清单待批准；可点选次责建议后随批准写入 | 批准 / 单项修订 / **删除（全局序号或 UUID）** / 全文重跑 |

HTTP 层暂停状态统一为 `AWAITING_REVIEW`，前端靠 `interrupt.type` 区分。

## 链路可视化

`astream(updates)` 只在节点结束后 flush 事件，所以**不在节点开头发 running**。

- 循环前发 `intake running`
- 非 HITL 节点结尾发：自己 `done` + 下一节点 `running` + 未走分支 `skipped`。无 Key 且无 owner ERROR 时 `propose_owners` 与 `assign_owners` 均为 `skipped`；有 Key 时 `propose_owners` 发 `done`，无 owner ERROR 则 `assign_owners` 为 `skipped` 并进入审核。
- HITL 不发自己 `done`；`waiting` 由检测到 interrupt 后覆盖

## 评测

`python -m evals.generate_cases` 用模板生成对话，产物冻结在 `backend/evals/golden/`。

每个 case 独立 `MemorySaver` + `graph_ns`。runner 循环注入 gold 的澄清 / 分配 / 批准，直到 `markdown_output` 非空或超 cap。未知 interrupt 类型直接失败。

门槛：

- 任务召回（`title_keywords` 重叠配对，不重复）≥ **0.9**
- 配对成功任务上主责 exact ≥ **0.8**
- 次责 set 相等 ≥ **0.8**
- HITL 触发与 `needs_clarify` / `needs_assignment` 一致 = **100%**

黄金集是 **规则路径回归**，不代表 LLM 抽取效果。`assign_04.json` 为单独提交的代词 case，不走 `generate_cases` 模板。

`evals/hard/` 是手写脏数据。三档（rules / 规则+LLM / 仅 LLM）打的是规则修复之后的底稿，不是最终人工分配结果。召回、精确率、证据有效率、负责人幻觉率共用这份列表；幻觉率对照的是补干系人名单之前的 stakeholders。下限在 `evals/hard_expected.json`，响应在 `evals/cassettes/`。重录 cassette 不会改下限。CI 用 respx 按完整 user 文本的 hash 回放，不打真 API，也不回放 validate_repair 的工具循环。复姓和弱动词在规则路径只上报数字，不把漏检写成失败。

前端 Demo 按钮为 **案例1 / 案例2 / 案例3**（陈述句纪要，无「参会人：」清单，句首不写「人名：」）。案例3 含「这个跟进交给她负责」，应进入 `assign_owners`。

## 规则路径失败分析

下列纪要 **不是** 模板生成，在 `LLM_PROVIDER=none` 下的表现。面试时主动讲边界，不要把黄金集 0.9 说成模型效果。

1. **复姓 / 英文名**：无「参会人：」清单时，规则用百家姓猜名字。欧阳、Alex 进不了干系人，后续主责会被打成 `INVALID_PRIMARY` 或直接缺主责。
2. **主题句带「推进」**：`推进` 既是动作词也是次责标记。例如「今天推进一下整体节奏」会被抽成任务。规则校验循环没有 `drop_items`，但审核阶段人可以按全局序号删掉（「第 2 条不是任务，删掉」）。按人看板不编号，避免和「张三那组的第 2 条」打架。
3. **「交给她」无法消解（预期成功）**：句中有 `她` 且没有干系人姓名 → `unresolved_ref` + `UNRESOLVED_REFERENCE`，先 `propose_owners`（规则路径建议为空）再进入人工分配。模型有 Key 时只给建议，不写入主责。这是功能，不是缺陷。
4. **职责写在后半句、动作词很弱**：「认证模块这周也得有人看一下」没有「负责/完成/跟进」等关键词，规则漏检。LLM 分块抽取可以补上；规则底稿不会被截断后的模型结果整表替换。
5. **一人多任务只抽到带关键词的分句**：同一发言里第二句若没有动作词，规则不会并成两项。案例1 里把人名和动作写在同一句，是为了让规则路径也能演示按人看板。
6. **超长 ASR**：无空行时按句号切块并保留 overlap。硬上限拒绝超大输入；软上限提示将分块抽取。删空后批准是合法终态，导出写「本次无任务」。
