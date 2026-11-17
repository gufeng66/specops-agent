# Checkpoint 模式与存活 / 就绪分开

## 背景

`backend/app/memory/checkpoint.py` 现在有两条隐式回退：`setup_checkpointer()` 打不开 SQLite 就 log 一条 exception 然后换 `MemorySaver`；`get_checkpointer()` 发现还没 setup 就直接 `MemorySaver()` 新建一个。

第一条把「持久化失败」降级成了「进程照常起来」。进程确实活着，但所有 checkpoint 都写进内存，重启即丢——用户的刷新恢复、`Command(resume=...)` 续跑、跨 HITL 的游标对齐全部失效，而 `/health` 仍然返回 200。这在单机本地演示里是便利，在需要"续得上"的场景里是静默的数据丢失。

第二条更糟：`get_checkpointer()` 每次未初始化就新建一个 `MemorySaver`，等于**每次调用拿到不同的存储**。写进去的游标下一次读不到，问题表现为"恢复时提示找不到 checkpoint"，排查方向会被彻底带偏。

而 `/health` 只有一个端点，同时承担了两个不该混的职责：告诉编排器「进程还活着」和告诉负载均衡「可以导流量给我」。

## 决策

> 以下写的是**决定要怎么做**，不等于已经做完；每一条的实际落地情况见文末「落地状态」。

### 1. 显式模式，不再静默回退

新增 `CHECKPOINT_MODE`，取值 `sqlite`（默认）| `memory`：

| 模式 | 含义 | 谁用 |
| --- | --- | --- |
| `sqlite` | 用 `AsyncSqliteSaver` 写 `CHECKPOINT_PATH` | 默认；本地持久化、Compose、CI 的跨重启用例 |
| `memory` | 用 `MemorySaver` | **必须显式选择**。不要求跨重启恢复的单测、一次性演示 |

`sqlite` 模式初始化失败（路径不可写、目录不存在、依赖缺失）→ **启动失败**，进程退出，端口不监听。不再回退到 `MemorySaver`。

`APP_ENV=production` 且 `CHECKPOINT_MODE=memory` → **拒绝启动**。生产环境里内存 checkpoint 不可能是有意选择，「为了先跑起来」的低成本做法必须在这里被挡住。

### 2. `get_checkpointer()` 未初始化即抛错

未 setup 时不再新建 `MemorySaver`，改为抛 `RuntimeError`。调用方要么在 lifespan 里先完成 setup，要么在测试里显式 `setup_checkpointer()` 后再用。全局只有一个进程级实例，`reset_checkpointer()` 仅用于单测清理。

### 3. `/health` 是 liveness，`/ready` 是 readiness

四个状态必须能被外部区分：

| 场景 | `/health` | `/ready` | 编排器行为 |
| --- | --- | --- | --- |
| `sqlite` 初始化失败 | —— （进程未起来，连不上） | —— 不可访问 | 起不来就是起不来，让启动失败暴露在部署日志里 |
| `memory` 显式选择，正常 | 200 | 200 + `degraded=true` | 可接流量，但 readiness 自报降级 |
| 已启动后依赖失效（SQLite 文件被删 / 连接坏 / 挂载丢失） | 200 | **503** | 从负载均衡摘除，但不重启进程 |
| 正常 | 200 | 200 + `degraded=false` | 正常 |

`/ready` 响应示例（正常态）：

```json
{
  "status": "ready",
  "degraded": false,
  "app_env": "development",
  "checkpoint_mode": "sqlite",
  "checks": [
    { "name": "business_db", "ok": true, "detail": "" },
    { "name": "checkpoint", "ok": true, "degraded": false, "detail": "" },
    { "name": "http_client", "ok": true, "detail": "" },
    { "name": "llm_config", "ok": true, "detail": "" }
  ]
}
```

`checks` 是**逐项结果数组**，不是固定键的对象：新增一类依赖时数组多一项，旧读取方不会因为字段改名而崩。`status` 只有 `ready` / `not_ready` 两个取值，降级不靠改 `status` 表达，而是 `degraded=true`——「能服务，但某个能力不持久」和「不能服务」是两件事，用一个枚举值同时表达会逼调用方去解析字符串。

依赖失效时 HTTP 503，body 结构不变，另外多一个 `detail` 字段，内容是失败项的名称与原因拼接（`"；".join(f"{name}: {detail}")`）。只在失败时给 `detail`，是因为逐项 `checks` 已经写明每项状态，成功路径上再拼一遍是冗余。

`APP_ENV` 一并显式化，因为「生产拒绝 memory」这条规则需要一个可信的判据，不能靠 `CHECKPOINT_MODE` 自己声明。默认值是 `development`。

### 4. 启动失败不用 503 表达

这是本节最容易被问的地方。503 的语义是「我现在处理不了，请等会儿再试」——它的前提是**进程在监听**，背后的假设是重试会有效。初始化失败时进程要么根本没起来，要么 checkpoint 永远建不出来；用 503 表达会得到一个永远 503 的健康进程，编排器按退避无限重试，而真正的错误被埋在一行 `/ready` 日志里。

更重要的是**能力差异**：一个 checkpoint 建不出来的进程，`/api/v1/chat/stream` 会在第一跳就失败，它没有任何可用功能。让它监听端口，只是在制造一个看起来很健康、实际全废的实例。

所以分界是：**启动期能力缺失 → 启动失败（fail fast）；运行期能力退化 → readiness 503（可恢复）**。前者是部署问题，后者是运行时抖动，处理手段完全不同。

## 取舍

**`memory` 模式仍保留，但只作为显式选择。** 大量单测不需要跨重启恢复，强制 SQLite 会让测试变慢并引入临时文件清理成本。代价是这个模式必须被 `APP_ENV=production` 挡住，否则它迟早会被拿去当生产配置。

**`memory` 模式 `/ready` 返回 200 而不是 503。** 它是用户明确选择的运行方式，不是故障；用 503 会让编排器摘掉一个按设计正常工作的实例。用 `degraded=true` 表达"能服务但不持久"是准确的：这个实例现在可以处理请求，重启后会话就没了。

**没有引入第三方健康检查库或指标导出。** 当前只有两个端点、四个状态，手写判断更直接。等真正接监控告警（见 README 的 Production Hardening Roadmap）时再考虑。

**`get_checkpointer()` 抛错会打破"import 期不崩"的便利。** 这是有意的：import 期的隐式 `MemorySaver` 让错误延后暴露到了更难定位的地方，宁可让初始化顺序问题在最靠近它的地方失败。

## 后果

- 部署时 `CHECKPOINT_PATH` 不可写 → 容器起不来，日志里是明确的初始化错误，而不是几小时后「重启后会话全丢」的悬案。
- 新增 `APP_ENV`、`CHECKPOINT_MODE` 两个配置项，`.env.example` 与 README 的环境配置表需要同步（已同步）；Compose 默认仍是 `CHECKPOINT_MODE=sqlite`，但 Compose 的 `APP_ENV` 默认是 `production`。
- 单测若要使用内存 checkpointer，必须显式 `CHECKPOINT_MODE=memory`（或在 fixture 里显式 setup），不能再依赖隐式回退。
- 运行期依赖失效需要一个探活手段：`/ready` 检查要能发现 SQLite 连接已坏，不能只反映启动时的状态快照。

## 落地状态

本文是设计约定，不是完成证明；以源码为准。最后一次核对时间：2026-11 审查整改。

> 本节按**符号名**定位（函数名、类名、模块路径），不写死行号：后端在整改期间仍在改动，行号会漂移，写死的数字比符号名更快过期。核对方式是在对应文件里检索符号名。

**已落地：**

- `CHECKPOINT_MODE` / `APP_ENV` / `REQUEST_RETENTION_DAYS` 三个配置项在 `backend/app/config.py` 中已存在（`checkpoint_mode` 默认 `sqlite`、`app_env` 默认 `development`、`request_retention_days` 默认 `7`）。
- 决策 1 的「`sqlite` 初始化失败 → 启动失败」：`setup_checkpointer()`（`backend/app/memory/checkpoint.py`）在 `except` 里先 `__aexit__` 关掉已开的连接再 `raise CheckpointSetupError`；lifespan（`backend/app/main.py`）不捕获它，进程起不来。
- 决策 2：`get_checkpointer()` 未初始化时抛 `CheckpointNotReadyError`，不再新建 `MemorySaver`。
- 决策 3：`/health` 只返回 `{"status": "ok", "llm_provider": ...}`（`backend/app/main.py` 的 `health()`），`/ready` 已实现（同文件的 `ready()` + `_readiness_checks()`），逐项探测 `business_db` / `checkpoint` / `http_client` / `llm_config` 四项，失败返回 503。`business_db` 用专门的 `MemoryStore.ping()`（`async with aiosqlite.connect(...)` + `SELECT 1`）做健康探测，不再借用 `list_running()` 这类业务查询——健康探测的语义不随某张表的查询逻辑变化，失败时 `ping()` 记日志并返回 `False` 而不是抛出。
- `memory` 模式启动时 `_degraded=True` 并打一条 warning（`checkpoint.py` 的 `setup_checkpointer()` memory 分支），`/ready` 的 `degraded` 由该项带出。
- **`CHECKPOINT_MODE` 是严格枚举**：`backend/app/config.py` 用 `checkpoint_mode: Literal["sqlite", "memory"]` 加 `@field_validator` 拒绝未知值（`CHECKPOINT_MODES = ("sqlite", "memory")`），`checkpoint_mode()` 也不再静默回退到 `sqlite`，非法值抛 `CheckpointSetupError`。拼错成 `sqltie` 会让进程起不来，而不是被当成 `sqlite` 悄悄跑。
- **启动失败的资源清理**：lifespan 把整个初始化（不只是 `yield`）包进 `try`，`finally` 无条件调 `reset_compiled_graph()` / `close_checkpointer()` / `stop_llm_runtime()`（三者各自幂等）。这样 `memory_store.init()` / `setup_checkpointer()` / 绑定图 / 对账中途失败时，已经起的共享 HTTP client 与 checkpoint 连接也会被关掉，不会泄漏。

**未落地（不要照本文宣称已具备）：**

- `APP_ENV` 默认值是 `development`，与本文早前设想的 `local` 不同；**取值域没有强制校验**——传入任意字符串都不报错。判定只做 `strip().lower()` 后与 `("production", "prod")` 比较，其余写法（如 `Production`、`prod ` 经归一后能命中，但 `prodcution`、`staging`、`prod-x` 之类）一律静默退化成「非生产」，即允许 memory。
- **没有定时健康巡查**：`/ready` 只在被调用时逐项探测，进程自己不会主动发现依赖失效并自愈，也没有「降级持续了多久」的指标或告警。降级只体现在响应体的 `degraded` 与 `checkpoint` 项的 `detail` 上。
- **`memory` 模式重启必然丢会话**：这是设计选择而非缺陷，但意味着崩溃恢复、滚动发布、多副本共享 HITL 状态这三种场景都不支持。

> 反向补充：原先记的「`APP_ENV=production` + `CHECKPOINT_MODE=memory` 拒绝启动未实现」已过期。`backend/app/memory/checkpoint.py` 新增 `_app_env()` 与 `_assert_mode_allowed()`，由 `setup_checkpointer()` 在开头调用，命中时抛 `CheckpointSetupError` 让进程起不来。

> 反向补充：原先记的「`.env.example` 未同步这三个键」已过期。`backend/.env.example` 现 19 行，末尾三行是 `APP_ENV=development`、`CHECKPOINT_MODE=sqlite`、`REQUEST_RETENTION_DAYS=7`；`docker-compose.yml` 也已传这三项（默认 `production` / `sqlite` / `7`）。注意 Compose 的 `APP_ENV` 默认是 `production`，所以在 Compose 里把 `CHECKPOINT_MODE` 改成 `memory` 会直接被拒绝启动——这是有意为之。

> 反向补充：原先记的「`/ready`、`CHECKPOINT_MODE`、`CheckpointSetupError` 没有回归用例」已过期。`backend/tests/test_readiness.py` 现有 17 个用例，覆盖：sqlite 初始化失败抛 `CheckpointSetupError`、memory 模式显式降级、未 setup 时抛错、`/ready` 报告 memory 降级、**`business_db` 由 `ping()` 探测**（`test_ready_returns_503_when_ping_fails`、`test_ping_succeeds_on_openable_db`、`test_ping_returns_false_on_unreadable_path`、`test_list_running_failure_does_not_change_ping_semantics`）、sqlite 空路径/`:memory:` 拒绝启动、production + memory 拒绝启动、LLM 配置非法拒绝启动、**`CHECKPOINT_MODE` 严格枚举**（`test_invalid_checkpoint_mode_is_refused`、`test_settings_rejects_invalid_checkpoint_mode_from_env`）、**启动失败也关闭已起资源**（`test_startup_failure_closes_runtime_even_before_yield`、`test_llm_config_failure_closes_runtime`）。
