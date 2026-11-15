# 快照版本、CAS 写入与请求回放窗口

## 背景

`MemoryStore.finish_request()` 现在按 `thread_id + lease_request_id` 更新线程行：租约还在、`request_id` 对得上，就整块覆盖 `snapshot_json`。

这条路径有两个洞。

**其一，覆盖是"最后写者赢"。** 同一线程的两段执行在时间上重叠时（前一段因超时/断连被判失败但协程没死、或者人工在旧页面上提交），后落盘的快照会把先落盘的整块顶掉。租约机制挡住了"并发开启新一趟"，但挡不住"旧的那趟回来得更晚"——它只保证同一时刻只有一个 `lease_request_id` 有效，而**失效租约的写入者是否已完全停止**是另一回事。

**其二，`processed_requests` 只增不减。** 每行存完整 `snapshot_json`，而快照包含 `source_text`、`markdown_output`、`task_draft`。会议原文动辄上万字，一个线程跑过几十次审核重放之后，这张表的体积远超 `threads` 表本身，且全是历史副本。另外 `find_by_run_id()` 当时没有任何索引可用，续跑反查是慢路径。

再叠加一个概念混淆：`schema_version` 和"这次快照是第几版"被当成同一个东西。前者是**读旧数据时要不要迁移**的问题，跨版本才变；后者是**并发写入该不该覆盖**的问题，每次写入都变。用一个字段同时表达两件事，两边都会错。

## 决策

> 以下写的是**决定要怎么做**，不等于已经做完；每一条的实际落地情况见文末「落地状态」。

### 1. 两个版本号，职责不重叠

| 字段 | 含义 | 变化频率 | 用途 |
| --- | --- | --- | --- |
| `schema_version: int` | 快照 JSON 的**数据结构**兼容版本 | 只在字段增删改时 +1 | 读旧数据时决定是否迁移、能否直接读 |
| `revision: int`（快照逻辑版本） | 同一线程快照内容的**逻辑版本**，单调递增 | 每次成功写入 +1 | CAS 前置条件、前端丢弃过期响应 |

两者不可互相替代。`schema_version` 相同不代表内容相同（可能是同一版本的两次不同写入）；`revision` 更大不代表结构更新（两次写入可能都是 `schema_version=1`）。数据库列名用 `snapshot_revision` 与内存字段 `revision` 区分，避免和 `schema_version` 在 SQL 里看串。

前端也用 `revision` 做快照校验：`acceptSnapshot` 拒绝 `next.revision < prev.revision` 的响应，让迟到的旧响应不能覆盖新状态。这与 AGENTS.md 第 5 节「迟到的旧响应不能覆盖新状态」是同一条规则的两个侧面。

### 2. `finish_request` 带 `expected_revision` 做 CAS

```python
async def finish_request(
    self,
    request_id: str,
    snapshot: ThreadSnapshot,
    *,
    request_status: Literal["succeeded", "failed"],
    expected_revision: int | None = None,
) -> FinishResult:  # 类型化结果，见下方说明
    # UPDATE threads
    #    SET status = ?, snapshot_json = ?, lease_request_id = NULL, updated_at = ?,
    #        run_id = ?, graph_ns = ?, snapshot_revision = snapshot_revision + 1
    #  WHERE thread_id = ? AND lease_request_id = ? AND snapshot_revision = ?
    # expected_revision 省略时退化为仅按 thread_id + lease_request_id 定位
```

`rowcount == 0` 时不覆盖、不回退，按 `request_id` 再查一次区分两种失败：

| 情况 | 判定 | 返回 |
| --- | --- | --- |
| 该 `request_id` 的租约已不在本线程（`lease_request_id` 不等于它） | 租约丢失 | 既有语义：该写入失效，请求标 `failed` |
| 租约还在，但 `snapshot_revision` 已被别人推进 | revision 冲突 | `SNAPSHOT_CONFLICT` |

`SNAPSHOT_CONFLICT` 的处理原则是**冲突不覆盖**：本次写入放弃，把 `SNAPSHOT_CONFLICT` 作为可诊断结果报出去（日志 + 请求状态），由上层决定是让用户重试还是直接把当前服务端快照返回给前端。绝不能"冲突了就再写一次"——那等于把 CAS 降级成重试，最后写者赢的问题原样回来。

`FinishResult` 已落地（m02853 第 5 项）：`@dataclass(frozen=True) class FinishResult` 带 `applied: bool` 与 `code: Literal["", "SNAPSHOT_CONFLICT", "LEASE_LOST"]`，并实现 `__bool__` 让既有 `if held:` 语义不变。`finish_request()` 返回它，0 行分支按 `lease_request_id` 归属给 `LEASE_LOST`（租约已丢）或 `SNAPSHOT_CONFLICT`（租约仍在、版本冲突），同时保留原来的两条可诊断日志。workflow 层用 `finished.applied` / `finished.code` 记 `snapshot write rejected code=...`，`reconcile_open_requests()` 同理。**注意语义优先级**：第一次成功写入会释放租约，此后同一请求的迟到写入先命中 `lease_request_id` 条件失败，判 `LEASE_LOST` 而非 `SNAPSHOT_CONFLICT`；纯粹的 `SNAPSHOT_CONFLICT` 只在租约仍持有时出现。回归用例见 `backend/tests/test_resilience.py::test_lease_loss_and_version_conflict_are_distinguishable` 与 `backend/tests/test_store_guards.py::test_finish_result_bool_matches_applied`。

CAS 失败时**不释放租约**（`lease_request_id` 保持原值），因为写入方没有完成这次更新，释放会让另一个请求挤进来，制造更难查的交叉覆盖。

### 3. `processed_requests` 的保留窗口

新增 `REQUEST_RETENTION_DAYS`，默认 `7`，`0` 表示关闭清理（无限保留，用于本地排查）。

回放能力分两档降级：

- **窗口内**：完整回放。同一 `request_id` + 同一操作摘要 → 返回首次结果快照。
- **窗口外**：`REQUEST_EXPIRED`。**元数据保留**（`request_id` / `thread_id` / `run_id` / `op` / `digest` / `status` / `created_at` 都不动），`snapshot_json` 换成合法空对象 `'{}'`——不是空串，因为读取路径会 `json.loads`，空串会让过期行直接把反序列化炸掉。

保留元数据而不是整行删除，是因为幂等判定的核心信息在里面：清掉行，一个过期 `request_id` 再来一次就会被当成全新请求，可能重复执行一个本该被识别为旧请求的操作。留着行、正文换成空对象，语义正好是"我知道你做过这件事，但结果已经不能还给你了"——这比"当没发生"安全。

回放窗口由认领事务严格执行：非 `running` 请求只要 `created_at` 早于 UTC cutoff（或时间戳无法安全解析），立即原子标记为 `expired` 并返回 `REQUEST_EXPIRED`。因此低流量不会延长可回放期。`REQUEST_RETENTION_DAYS=0` 明确关闭自动过期与清理。

物理清理只回收正文：启动时和每小时维护任务调用 `prune_processed_requests()`，按固定 cutoff、每批 500 行循环，直到无剩余或达到短时预算。每轮记录清理数量、剩余数量和停止原因。任何情况下都保留 `request_id`、摘要和状态等幂等墓碑，绝不删除整行。

### 4. `run_id` 反查走 `threads.run_id` 索引

`find_by_run_id()` 要回答的是「这个 `run_id` 现在是哪条线程在跑」，供 `/api/v1/runs/{run_id}/resume` 反查线程用。所以它查的表是 `threads`，不是 `processed_requests`：

```sql
CREATE INDEX IF NOT EXISTS idx_threads_run_id ON threads(run_id);
```

索引在 `init()` 里随建表一起 `IF NOT EXISTS` 幂等创建。`run_id` 在 `threads` 上不唯一（不是每个线程都只有一个历史 run），因此建普通索引，不能建唯一索引。

这里有个语义要点：`threads.run_id` 是**线程当前的运行标识**，不是「run → thread 的历史映射」。续跑成功一次就推进一次，旧 `run_id` 不再指回该线程。这正是 `/runs/{run_id}/resume` 需要的语义——只有当前 run 才能续跑，旧 run 号的提交必须被拒。如果将来需要「任意历史 run 都能查到当时属于哪条线程」，那要另建映射表，不能靠这一列。

本文件早先写过「给 `processed_requests.run_id` 建 `idx_processed_requests_run_id`」，那是把反查表选错了：`processed_requests` 里同一 `run_id` 可能有多条 resume 记录，且快照正文已被清理策略清空，不适合做反查源。该索引最终没有建，也不建。

`find_by_run_id()` 的实现是先 `SELECT ... FROM threads WHERE run_id = ? LIMIT 1`，未命中时再对 `run_id IS NULL` 的行做一次兜底扫描——那些是迁移前写入、当时还没有 `run_id` 列的旧行。兜底只覆盖历史数据，新写入都会填 `run_id`。

## 取舍

**为什么不直接用 `updated_at` 当 CAS 版本？** 时间戳有分辨率问题（同一毫秒内的两次写入相等）、有时钟回拨问题、且 `updated_at` 被多处写入逻辑修改，当版本号会产生非单调。整数 `revision` 单调性由数据库的 `revision = revision + 1` 保证，不需要额外假设。

**为什么冲突返回错误码而不是静默重试？** 静默重试会把"两个写入者都认为自己是最后一个"的竞态藏在一次多余的成功里。显式冲突至少能让日志出现一条可 grep 的记录。代价是调用方必须处理这个返回，不能当布尔值用。

**为什么过期只清正文、不删行？** 见上。代价是表仍然会长，只是从"保留全文"降到"保留索引级元数据"。真要压缩体积，下一步是定期把过期行搬到冷表，但那需要迁移和读取路径两处改动，不在当前范围。

**为什么 `REQUEST_RETENTION_DAYS=0` 表示关闭而不是"立即过期"？** 用 0 表示"禁止保留"同样自洽，但会让本地排查变成"刚跑完就查不到"。选择"关闭"语义是因为这个配置的第一使用场景是"我不想自动删东西"，而不是"我要删得更狠"。

## 后果

- `ThreadSnapshot.revision` 必须由存储层自增，业务代码不能在内存里手工 `+1` 再传进来——那样两个写入者会算出同一个期望值。
- 任何新增的写快照路径都要带 `expected_revision`，漏传就等于绕过 CAS。这条已由 `save_thread()` 的运行时拦截兜住（无版本号更新已有线程直接抛 `StaleSnapshotError`），并有 `test_concurrent_writes_same_expected_revision_only_one_wins` 覆盖真并发。
- `SNAPSHOT_CONFLICT` / `LEASE_LOST` 是数据层诊断码，不是新的 HTTP 错误码。HTTP 层怎么映射（重试提示还是直接返回当前快照）留给 API 层决定。二者已通过 `FinishResult.code` 类型化返回，调用方可用 `.applied` / `.code` 区分，日志仍保留两份可诊断文案。
- 过期回放返回 `REQUEST_EXPIRED` 后，前端要能区分"从没做过"和"做过但结果过期"，否则用户会重复提交。
- `init()` 的建表段变长，但全部是 `IF NOT EXISTS`，对已有库无破坏。`idx_processed_requests_run_id` 未建：反查改走 `threads.run_id` 上的 `idx_threads_run_id`。

## 落地状态

本文是设计约定，不是完成证明；以源码为准。最后一次核对时间：2026-11 审查整改收尾。

> 本节按**符号名**定位（函数名、SQL 片段、索引名），不写死行号：`store.py` 在整改期间仍在改动，行号会漂移。核对方式是在 `backend/app/memory/store.py` 中检索符号名或 SQL 片段。

**已落地：**

- `ThreadSnapshot.schema_version` 与 `ThreadSnapshot.revision` 两个字段（`backend/app/domain/models.py`），数据库列名 `snapshot_revision` 通过 `_ensure_column` 迁移补齐（`backend/app/memory/store.py`）。
- 决策 2 的 CAS：`finish_request(..., expected_revision: int | None = None) -> FinishResult` 在给定 `expected_revision` 时追加 `AND snapshot_revision = ?`；`rowcount == 1` 才写入并 `snapshot_revision = snapshot_revision + 1`，返回 `FinishResult(applied=True, code="")`；0 行时不覆盖、不释放租约、只把 `processed_requests` 对应行标 `failed`，返回 `FinishResult(applied=False, code=...)`（`LEASE_LOST` 或 `SNAPSHOT_CONFLICT`）。`FinishResult.__bool__` 返回 `applied`，既有 `if held:` 语义不变。
- 调用侧已带上 `expected_revision=snapshot.revision`：`backend/app/agent/workflow.py` 的 `_persist_aligned` 与 `_stream_graph` 的 interrupt 分支。
- **`save_thread()` 也强制 CAS**：签名是 `save_thread(snapshot, *, expected_revision: int | None = None)`。更新「已有线程」必须带 `expected_revision`；省略版本号时只允许「新建线程」，若线程已存在则先 `rollback()` 再抛 `StaleSnapshotError`。给定时先读 `snapshot_revision`：行不存在且 `expected_revision != 0` → 返回 `False`；`current != expected_revision` → 返回 `False`；否则 `snapshot.revision = current + 1`。这条把「漏传版本号就等于绕过 CAS」从约定变成了运行时拦截。
- **`schema_version` 越界双向拒绝**：`_assert_schema_version(snapshot)` 在 `int(snapshot.schema_version) > SNAPSHOT_SCHEMA_VERSION` 时抛 `PersistedStateTooNew`（消息说明「请升级服务后再读写，避免旧代码覆盖新结构」）；`_load_snapshot()` 解析出快照后立即调用它，因此**写入路径和读取路径都会拦**。这是「旧代码不许覆盖新结构」的兜底，不是版本迁移机制。
- 决策 3 的保留窗口：`claim_operation()` 在同一事务内判断有效时间，窗口外将快照正文置为 `'{}'` 并返回 `REQUEST_EXPIRED`；`prune_processed_requests()` 使用 `batch=500` 循环回收积压正文，保留墓碑。
- 生命周期在启动时清理积压并启动单一小时维护任务；任务保存在 `app.state`，关闭时取消并等待。请求路径轻量清理仍只是额外维护，不承担窗口语义。
- `find_by_run_id()` 已改为走 `idx_threads_run_id`，对 `run_id IS NULL OR run_id = ''` 的旧行兜底扫描。

**已落地（类型化诊断码）：**

- `FinishResult` **已是返回类型**：`finish_request` 返回 `FinishResult(applied, code)`，`code` 为 `""` / `SNAPSHOT_CONFLICT` / `LEASE_LOST`。0 行分支仍读回 `lease_request_id` / `snapshot_revision` 记两种可诊断日志（租约已丢记 `SNAPSHOT_CONFLICT lease lost`，租约仍在但版本不符记 `SNAPSHOT_CONFLICT thread=... expected=... actual=...`）。workflow 层用 `finished.applied` / `finished.code` 记 `snapshot write rejected code=...`，`reconcile_open_requests()` 同理。语义优先级：第一次成功写入会释放租约，迟到写入先命中 lease 条件失败 → 判 `LEASE_LOST`。用例见 `backend/tests/test_resilience.py::test_lease_loss_and_version_conflict_are_distinguishable`、`backend/tests/test_store_guards.py::test_finish_result_bool_matches_applied`。

**未落地（不要照本文宣称已具备）：**

- CAS 写入被拒绝后，workflow 不发送旧 `interrupt` 或 `result`，而是以 `SNAPSHOT_CONFLICT` / `LEASE_LOST` SSE error + done 收尾；前端重新读取权威线程快照。流开始前的认领拒绝仍是 HTTP 409。
- 「并发写入只有一个成功」**已有真并发用例**：`backend/tests/test_store_guards.py::test_concurrent_writes_same_expected_revision_only_one_wins` 用 `asyncio.gather` 让两个写入者持同一 `expected_revision` 竞争，断言只有一个成功。同一文件的 `test_save_thread_without_revision_cannot_update_existing`、`test_save_thread_stale_expected_revision_returns_false`、`test_schema_version_too_new_is_refused_on_write_and_read`、`test_prune_writes_empty_json_object` 分别覆盖 `save_thread` CAS、过期版本、`schema_version` 双向拒绝、`'{}'` 清理。

> 反向补充：原先记的「`REQUEST_RETENTION_DAYS` 在生产路径上没有调用点」与「`reconcile_open_requests()` 未传 `expected_revision`」都已过期。前者见上文两个触发点；后者现在传 `expected_revision=snap.revision`，CAS 不匹配时记 `reconcile CAS mismatch ... releasing stale lease` 后不带版本号重放一次，这是刻意保留的对账例外（`finish_request` 自身带 `lease_request_id` 条件，重放不会覆盖别人的写入）。
