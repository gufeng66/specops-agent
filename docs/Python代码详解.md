# Python 源码逐文件精读

> 本文是 [`项目详解.md`](./项目详解.md) 的配套篇：只讲 **每一个 `.py` 文件**。  
> 每个文件都用「**总**（这个文件干什么、谁在用）→ **分**（每个类/函数：输入输出、逐步怎么走、关键分支、为什么这样写）」。  
> 不包含 `.venv`、测试缓存。阅读时建议对照源码打开对应文件。

**建议阅读顺序：** `models.py` → `extractors.py` / `validation.py` → `graph.py` / `nodes.py` → `workflow.py` → `chat.py` / `store.py` → 其余。

---

## 目录（全部 29 个 py）

**应用入口与配置**

1. [`backend/app/main.py`](#1-backendappmainpy)
2. [`backend/app/config.py`](#2-backendappconfigpy)
3. [`backend/app/errors.py`](#3-backendapperrorspy)

**领域层**

4. [`backend/app/domain/__init__.py`](#4-backendappdomain__init__py)
5. [`backend/app/domain/models.py`](#5-backendappdomainmodelspy)
6. [`backend/app/domain/extractors.py`](#6-backendappdomainextractorspy)
7. [`backend/app/domain/validation.py`](#7-backendappdomainvalidationpy)

**Agent**

8. [`backend/app/agent/state.py`](#8-backendappagentstatepy)
9. [`backend/app/agent/graph.py`](#9-backendappagentgraphpy)
10. [`backend/app/agent/nodes.py`](#10-backendappagentnodespy)
11. [`backend/app/agent/tools.py`](#11-backendappagenttoolspy)
12. [`backend/app/agent/events.py`](#12-backendappagenteventspy)
13. [`backend/app/agent/prompts.py`](#13-backendappagentpromptspy)
14. [`backend/app/agent/workflow.py`](#14-backendappagentworkflowpy)

**API**

15. [`backend/app/api/schemas.py`](#15-backendappapischemaspy)
16. [`backend/app/api/chat.py`](#16-backendappapichatpy)
17. [`backend/app/api/review.py`](#17-backendappapireviewpy)
18. [`backend/app/api/threads.py`](#18-backendappapithreadspy)

**存储与 LLM**

19. [`backend/app/memory/store.py`](#19-backendappmemorystorepy)
20. [`backend/app/memory/checkpoint.py`](#20-backendappmemorycheckpointpy)
21. [`backend/app/llm/errors.py`](#21-backendappllmerrorspy)
22. [`backend/app/llm/client.py`](#22-backendappllmclientpy)

**评测**

23. [`backend/evals/generate_cases.py`](#23-backendevalsgenerate_casespy)
24. [`backend/evals/runner.py`](#24-backendevalsrunnerpy)
25. [`backend/evals/score.py`](#25-backendevalsscorepy)

**测试**

26. [`backend/tests/test_workflow.py`](#26-backendteststest_workflowpy)
27. [`backend/tests/test_api.py`](#27-backendteststest_apipy)
28. [`backend/tests/test_llm_client.py`](#28-backendteststest_llm_clientpy)
29. [`backend/tests/test_eval_golden.py`](#29-backendteststest_eval_goldenpy)

---

## 1. `backend/app/main.py`

**总：** FastAPI 应用的大门。Uvicorn 命令 `uvicorn app.main:app --port 8001` 加载的就是这里的 `app` 对象。它做四件事：启动时准备数据库和图、关掉时释放资源、挂上 CORS 和三条路由、提供健康检查。

**谁在用：** 生产启动、`tests/test_workflow.py` 的 `from app.main import app`、`tests/test_api.py` 用 ASGI 打真实路由。

### 分：`lifespan`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await memory_store.init()
    checkpointer = await setup_checkpointer()
    graph = bind_compiled_graph(checkpointer)
    from app.agent.workflow import SpecOpsWorkflow
    from app.api import chat as chat_mod
    from app.api import review as review_mod
    chat_mod.workflow = SpecOpsWorkflow(memory_store, graph=graph)
    review_mod.workflow = SpecOpsWorkflow(memory_store, graph=graph)
    try:
        yield
    finally:
        reset_compiled_graph()
        await close_checkpointer()
```

逐步：

1. `memory_store.init()`：在 SQLite 里 `CREATE TABLE IF NOT EXISTS` 两张表（线程快照、已处理请求）。没有这一步，第一次 `save_thread` 会报「表不存在」。
2. `setup_checkpointer()`：打开 LangGraph 的 SQLite 存档（失败则内存版）。这是 HITL 能「暂停后再续」的前提。
3. `bind_compiled_graph(checkpointer)`：用这份存档编译状态图，放到进程单例里。
4. **重新赋值 API 模块里的 `workflow`。**  
   `chat.py` / `review.py` 在被 import 时就会执行 `workflow = SpecOpsWorkflow(memory_store)`。那时 lifespan 还没跑，图可能是临时 MemorySaver。这里覆盖成「带真实 checkpointer 的图」，HTTP 请求才能跨进程重启恢复（至少跨请求恢复）。
5. `yield`：应用开始对外服务。
6. `finally`：丢掉编译图、关闭 SQLite 连接，避免测试或热重载泄漏句柄。

为什么 `from app.api import chat as chat_mod` 写在函数里面？避免循环 import：`main` import `chat`，`chat` 又 import `workflow`；lifespan 里再拿模块对象改属性是安全的。

### 分：创建 `app` 与中间件

```python
app = FastAPI(title="SpecOps Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, ...)
app.include_router(chat_router)
app.include_router(review_router)
app.include_router(threads_router)
```

- `lifespan=lifespan` 把上面的启动/关闭钩上。
- CORS：前端 `localhost:5173` 和 API `8001` 不同源，浏览器会拦截。`allow_origins` 来自环境变量逗号列表。
- 三个 router 各自带前缀（`/api/v1/chat`、`/api/v1/runs`、`/api/v1/threads`）。

### 分：`health`

```python
@app.get("/health")
async def health():
    return {"status": "ok", "llm_provider": settings.llm_provider}
```

不查数据库、不跑图。返回当前配置的 provider，方便确认「现在是规则路径还是 DeepSeek」。测试 `test_health` 只断言 200。

---

## 2. `backend/app/config.py`

**总：** 环境变量的唯一入口。全项目 `from app.config import settings`，不要在业务代码里自己 `os.getenv`。

**谁在用：** CORS、LLM 客户端、SQLite 路径、工具循环上限、checkpointer 路径。

### 分：`Settings` 类

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    llm_provider: str = "none"
    deepseek_api_key: str = ""
    ...
    max_tool_rounds: int = 4
    max_consecutive_tool_failures: int = 2
    max_tool_stall: int = 2
```

逐步：

1. `BaseSettings` 会按「环境变量名 = 字段名大写」自动绑定：`llm_provider` ← `LLM_PROVIDER`。
2. `env_file=".env"`：本地把 `backend/.env.example` 复制为 `.env` 即可，不必 export。
3. `extra="ignore"`：`.env` 里多写了未知键不会崩溃。
4. 默认 `llm_provider="none"`：CI 没 Key 也能跑；本地要模型必须显式改。
5. 三个 `max_tool_*` 只作用于 `nodes.py` 里 LLM 校验循环，防止模型反复调用同一个修不好的工具。

### 分：`cors_origin_list`

```python
@property
def cors_origin_list(self) -> list[str]:
    return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
```

把 `"http://localhost:5173,http://frontend:5173"` 切成列表。空段丢掉。Docker Compose 会配两个 origin。

文件末尾 `settings = Settings()`：**模块 import 时就读一次**。pytest 里若要改配置，必须 `monkeypatch.setattr("app.llm.client.settings.llm_provider", "deepseek")` 改的是**已被引用的那个对象的字段**，光改环境变量不一定生效（对象已经构造完了）。

---

## 3. `backend/app/errors.py`

**总：** 业务异常，目前只有「线程忙碌」。和 HTTP 层解耦：store 抛 `ThreadBusyError`，API 再转 409。

### 分：`SpecOpsError`

基类，带 `message` 和 `code`（默认 `SPECOPS_ERROR`）。以后若加别的业务错误可以继续继承。

### 分：`ThreadBusyError`

```python
class ThreadBusyError(SpecOpsError):
    def __init__(self, thread_id: str):
        super().__init__(f"线程 {thread_id} 正在处理中，请等待当前流程完成后再试", "THREAD_BUSY")
        self.thread_id = thread_id
```

在 `MemoryStore.claim_request` 里抛出：该线程状态是 `planning` 或 `awaiting_review` 时，不允许用新的 `request_id` 再开一趟（澄清恢复是 API 层故意不传 `thread_id` 给 claim，见 `chat.py`）。

### 分：`thread_busy_http`

```python
def thread_busy_http(thread_id: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "THREAD_BUSY", "message": ..., "thread_id": thread_id})
```

FastAPI 会把 `detail` 原样放进 JSON。前端 `sse.ts` 看到 409 后读 `err.detail.message`。必须是 409 而不是 400：语义是「冲突，过会儿再试」，不是「你参数写错了」。

---

## 4. `backend/app/domain/__init__.py`

**总：** 包的对外窗口。让别人可以 `from app.domain import ActionItem, validate_task_draft`，而不必记得文件名。

### 分：导出清单

```python
from app.domain.models import ActionItem, MeetingBrief, TaskDraft, ValidationIssue, ValidationResult
from app.domain.validation import apply_repairs, repair_item, validate_task_draft

__all__ = [ ... ]
```

**没有**导出 `extractors`。抽取函数名字更「脏」（正则、标题裁剪），调用方必须显式 `from app.domain.extractors import rule_decompose`，避免把规则细节当成稳定公共 API。

---

## 5. `backend/app/domain/models.py`

**总：** 全项目的数据合同。Pydantic 模型 = 运行时校验 + JSON 序列化（存 SQLite、发 SSE `result`、喂 LLM schema）。改字段会同时影响前端 TypeScript 接口、校验、导出，是最需要小心的文件。

### 分：`EvidenceKind`

枚举：`source`（引用必须是原文子串）/ `inference`（必须写推断理由，引用应为空）。校验逻辑在 `validation.py` 按这个分支。

### 分：`MeetingBrief`

四个字符串/列表字段，全部有默认空值。intake 即使什么都没抽到，也能构造出合法对象，然后 `needs_clarification` 看 `goal` 是否空。

### 分：`ActionItem`

一条任务。逐步注意：

1. `id: str = Field(default_factory=lambda: str(uuid4()))`  
   每次 new 一个任务都有唯一 id。HITL 分配、单项修订、前端选中都靠这个 id，**修订时不能重新生成**（见 `patch_target_item`）。
2. `strip_strings` 校验器（`mode="before"`）  
   在类型转换前对若干字符串字段 `strip()`。LLM 经常在人名前后加空格，不 strip 会导致「张伟 」对不上干系人「张伟」，误报 `INVALID_PRIMARY`。
3. `strip_secondary_owners`  
   列表里丢掉非字符串和空串，避免 `["", "李娜"]` 这种脏数据。
4. `unresolved_ref`：代词原文，如 `"她"`。有值且没有主责 → 校验报 `UNRESOLVED_REFERENCE`。
5. `priority` 默认 `"medium"`，规则路径几乎不填高低，模型路径可能会填。

### 分：`TaskDraft`

`items` + `version`。`apply_owner_assignments` / `apply_repairs` / `drop_items_tool` / `patch_target_item` 每次都会 `version + 1`。目前没有按 version 做乐观锁，主要方便日志和调试「修过几轮」。

### 分：`IssueSeverity` / `ValidationIssue`

- `ERROR`：能阻断「直接进审核」（负责人类会先去分配）。
- `ADVISORY`：目前只有缺截止日期，不挡流程。
- `item_id` / `field` 可空：`EMPTY_TASK_LIST` 是整份草案的问题，没有对应某一条。

### 分：`ValidationResult`

三个 `@property`：

- `error_issues` / `advisory_issues`：按严重级别过滤。
- `has_blocking_errors`：有没有 ERROR。注意：`finalize` **不再**用它拦截；分配满 3 次后带着 ERROR 也能进审核、批准、导出。

`repair_attempts`：规则路径记修复轮次；LLM 路径记工具循环 rounds。

### 分：`ThreadStatus`

`idle → planning → awaiting_review → completed`。HTTP 层把三种 HITL 都标成 `awaiting_review`，细类在 `interrupt_payload["type"]`。

### 分：`ReviewInfo`

审核醒来后写入 state，再映回快照。`revision_scope` 只能是 `item` 或 `document`。

### 分：`ThreadSnapshot`

给人和数据库看的「完整照片」。字段里特别要记住：

- `interrupt_payload`：当前暂停时前端要渲染的 JSON（澄清文案、待分配任务、干系人名单……）。
- `graph_ns`：LangGraph checkpointer 的 thread_id。澄清 resume **必须沿用**，否则 `Command(resume=...)` 找不到档。
- 快照不再带外部同步状态。写入 Jira / 飞书仍是未实现项。
- `markdown_output`：finalize 才填充；导出接口若为空会现场再渲染一份。

### 分：`ActionItemList` / `MeetingBriefExtract`

LLM `response_format=json_object` 的外壳。模型必须输出 `{"brief": {...}}` 或 `{"items": [...]}`，`chat_json` 才能 `model_validate`。没有这层，根上直接是 Brief 字段时 schema 对不上。

---

## 6. `backend/app/domain/extractors.py`

**总：** 规则路径的核心（约 510 行）。无 API Key 时，会议目标和任务几乎全靠这里。有 Key 时仍先跑一遍规则，再视情况被模型结果替换。文件后半还有人工分配补丁和 Markdown 导出。

**谁在用：** `nodes.py`（intake/decompose/assign/review）、`tools.py`（export）、`threads.py`（现场渲染）、大量测试。

---

### 分：常量

| 名字 | 作用 |
| --- | --- |
| `PRIMARY_ROLE_MARKERS` | 「负责/统筹/主责/牵头」→ 主责侧 |
| `SECONDARY_ROLE_MARKERS` | 「配合/协助/推进/支持/参与」→ 次责侧 |
| `PRONOUNS` | 消解失败时写入 `unresolved_ref`。顺序是「他们」在「他」前面，避免先匹配到单字。 |
| `ACTION_KEYWORDS` | 句子里没有这些词，`rule_decompose` 直接跳过（寒暄进不了任务）。 |
| `DEADLINE_PATTERN` | 年-月-日、月日、下周五、本周、月底等 |
| `TITLE_MARKERS` | 剪标题时的切断点：`由` + 主次责标记 |
| `GENERIC_ACCEPTANCE_RE` | 识别系统自动填的验收套话 |

「推进」既在动作词里又在次责标记里：主题句「今天推进一下整体节奏」可能被抽成任务。这是规则路径已知误伤，LLM 模式可用 `drop_items` 删掉。

---

### 分：`_has_action_keyword(sent)`

```python
if kw == "负责":
    if re.search(r"负责(?!人)", sent):
        return True
    continue
```

「负责人」是职位，不能当成「有人负责去做」。负向先行断言 `(?!人)`：后面不是「人」才算动作词。其它关键词用 `in sent` 即可。

---

### 分：`_is_generic_acceptance` / `GENERIC_ACCEPTANCE_RE`

匹配两种自动生成句：

- `「xxx」已完成并通过确认`（decompose 默认验收）
- `完成「xxx」并经过干系人确认`（repair 填的验收）

**当前 `render_markdown` 并不调用它。** 导出时按人分节的任务行本来就不含验收字段，所以套话不会出现在正文——测试 `test_rule_extract_brief_topic_header` 断言 `"已完成并通过确认" not in body`。这个函数留给测试和将来若把验收打进 Markdown 时过滤。

---

### 分：`_strip_deadline(text, deadline)`

从标题候选里抠掉截止日期，并吃掉紧挨着的「之前/之内/前/内/后」。

例：`张伟下周五前完成认证重构`，deadline=`下周五` → 去掉「下周五」和后面的「前」。  
然后再 `re.sub(r"截止时间为?", "")` 清掉「截止时间为」字样，strip 标点。

若 `deadline` 不在 text 里，原样返回。

---

### 分：`_earliest_marker_index(text)`

在 text 里找所有 `TITLE_MARKERS` 第一次出现的位置，返回最小下标。「负责」若实际是「负责人」则跳过这个命中、从下一个字符继续找。找不到返回 `None`，`_short_title` 会改走动作词切断。

---

### 分：`_short_title(sent, deadline="")`

规则路径的标题生成，目标是短、不含截止、不含「由谁负责」。

逐步：

1. `fallback = sent[:50]`，万一剪完变空就用它。
2. 去掉句末 `。；;`，再 `_strip_deadline`。
3. 若有角色标记，在最早标记处切断（「会议组织任务由周婷负责…」→「会议组织任务」）。
4. 否则找动作词（位置必须 `> 0`，句首就是「负责」则不断）。同样跳过「负责人」。
5. 循环去掉后缀「工作/任务/事项/安排」（「会议组织任务」→「会议组织」）。
6. 去掉前缀「本次/这次/关于」。
7. 再剥「 ，,、的」，截断到 50 字。

测试锚点：`本次会议组织任务由周婷负责，截止时间为2026年8月7日17:30。` → `会议组织`。

---

### 分：`rule_extract_brief(source_text) -> MeetingBrief`

无模型时的 intake。按行扫描，互不依赖顺序。

**Goal：** 正则  
`(会议主题|会议目标|目标|议题)[：:]` 或 `(讨论|关于)[：:]`。只填第一次命中。

**日期：** `(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)`，再把年月日换成 `-`。

**干系人，三条来源：**

1. 页眉名单：`参会人|参与人|干系人|与会|出席|会议负责人` 后面按顿号/逗号/空格切开。
2. 发言人：`说话人：张伟` 或 `张伟：正文`（2～4 个汉字 + 冒号）。排除「会议主题」「参会人」等伪姓名。
3. 百家姓猜测：`[张李王刘…][\u4e00-\u9fff]{1,2}` 且后面是「负责/统筹/配合/牵头/主责/须/和/顿号/句号」等。  
   **欧阳、Alex 进不了这条。** 没有页眉名单时会缺人，后续主责变 `INVALID_PRIMARY`。

最后 `dict.fromkeys` 保序去重，最多 12 个。

**context：** 不含「参会/目标/议题/日期/时间」的行，最多拼 5 行，给模型当背景（规则路径后续基本不用）。

---

### 分：`needs_clarification(brief) -> bool`

`not brief.goal.strip()`。空格也算没目标。`route_after_intake` 和 intake 发 skipped/running 都靠它。

---

### 分：`_split_clause(sent, marker)`

在 `marker` **第一次出现处**切开，前半含 marker 本身，后半是后面的文字。用于「统筹」句和「配合」句拆开：

`新产品上线工作由周婷统筹，孙磊、邓凯和马超配合`  
marker=`配合` → 前半到「配合」之前的「统筹」侧，后半是「孙磊…」。

---

### 分：`extract_owners_from_sentence(sent, stakeholders)`

**只认已经在干系人列表里的姓名**（`name in sent`）。正文里出现「外人」但名单没有，这里抽不到，校验阶段再报 `INVALID_PRIMARY`。

分支：

1. 名单里一个都没提到 → `("", [])`。
2. **主责标记和次责标记都有：** 在第一个次责标记处切开。前半命中的第一个当主责，后半当次责；漏网的人名补进次责。若前半没人名、后半有：尝试把「不在后半」的人当主责，否则退化成后半第一个当主责。
3. **只有主责标记：** 第一个主责，其余次责。
4. **只有次责标记且超过 1 人：** 仍把第一个当主责（「A 配合 B」这种没法从标记判断谁主责，规则只能猜）。
5. **只有 1 人：** 就是主责。
6. **多人但没有任何角色标记：** 第一个主责，其余次责。

最后次责去重，且不能等于主责。

---

### 分：`detect_unresolved_ref(sent, stakeholders)`

若句子里已经有任一干系人姓名 → 返回 `""`（认为指代可解，或根本不用代词）。  
否则按 `PRONOUNS` 顺序找，命中就返回该代词。

`rule_decompose` 里一旦有 unresolved，会**强制清空**已抽出的主次责，避免「她」被误配成碰巧出现在别处的人名。

---

### 分：`rule_decompose(source_text, brief) -> TaskDraft`

逐步：

1. 丢掉页眉行（会议主题/参会人/日期…）。
2. 去掉 `说话人：` 或 `张伟：` 前缀，按 `。` 切开得到句子列表。
3. 每句：没有动作词或长度 &lt; 4 → 跳过。
4. 抽主次责；若有代词未消解，主次责清空。
5. 正则找截止日期。
6. `_short_title` 做标题。
7. **证据对齐：** `quote` 默认整句。若整句不是 `source_text` 的子串（例如切句子时丢掉了句号），依次试 `去句号` / `加句号`；再不行就找「含相同动作词的整行」。校验要求 `quote in source_text`，这里必须尽力对齐。
8. 验收填模板 `「{title}」已完成并通过确认`。
9. `evidence_kind=SOURCE`。

不合并同一人的多句话：一句最多一项。所以案例1 必须「人名和动作在同一句」，否则第二句没动作词会被丢掉。

---

### 分：`patch_target_item(draft, item_id, feedback)`

审核「单项修订」：

- 其它任务 `model_copy(deep=True)`，**id 不变**。
- 目标项：描述末尾追加 `\n[修订反馈] ...`；验收若还没有「（已修订）」后缀就加上。
- `version + 1`。

它**不**根据反馈内容改标题或负责人，只留痕迹，然后图回到 `validate_repair` 再验一遍。

---

### 分：`apply_owner_assignments(draft, assignments)`

`assignments` 是 dict 列表，来自前端。逐步：

1. 先按 id 深拷贝所有任务到 `by_id`（保证未提到的项原样保留）。
2. 对每条 raw：id 不在草案里则忽略（防乱传）。
3. 若 key 含 `primary_owner`：写入；非空则 `unresolved_ref=""`（人已经指定，代词问题消失）。
4. `secondary_owners` 可以是字符串或列表。
5. 按原 `by_id` 的插入顺序输出（Python 3.7+ dict 保序）。

只改出现在 payload 里的项：这就是「部分分配后还要再 interrupt」的基础——你只填了 2 条里的 1 条，另一条仍缺主责。

---

### 分：`render_markdown(...)`

导出给人看的 Markdown，不是给机器的 JSON。

结构：

1. 标题「会议纪要」+ 目标/日期/干系人/任务数。
2. 收集「所有出现过的人名」（brief 干系人 + 各任务主次责），按第一次出现顺序。
3. 对每个人：主责列表 + 「我是次责但不是主责」的配合列表。有主责则节标题 `## 张伟（主责 n）`，否则 `## 张伟（配合 n）`。
4. 主责行：`1. **标题** · 截止 · 配合：A、B`。配合行：`配合 · 标题（主责xxx）`。
5. 没有任何人名时，退化为「## 任务清单」编号列表。
6. 仍无主责且没被归到任何人的 → 「## 待分配」，代词任务会标「未消解：她」。
7. 校验 issues → 「## 未解决问题」，ERROR 用 ❌，advisory 用 ⚠️。
8. 文末字数 + `<details>` 折叠原文。

**故意不把验收套话打进按人列表**，所以通用验收不会污染正文。

---

## 7. `backend/app/domain/validation.py`

**总：** 质检员。决定「能不能直接审核」以及「规则允许自动修什么」。负责人相关错误**禁止自动修**，必须 HITL。

---

### 分：`OWNER_ERROR_CODES`

frozenset，五个码。`has_owner_errors` 只看 `error_issues` 里是否出现这些码（advisory 不算）。`nodes.py` 用它决定去 `assign_owners` 还是 `review`。`repair_item` 遇到这些码直接原样返回。

---

### 分：`has_owner_errors(validation)`

一行：`any(i.code in OWNER_ERROR_CODES for i in validation.error_issues)`。

---

### 分：`validate_task_draft(draft, brief, source_text)`

空列表先记 `EMPTY_TASK_LIST`。然后对每个 item：

| 条件 | 码 | 级别 |
| --- | --- | --- |
| 标题空 | `MISSING_TITLE` | ERROR |
| 验收空 | `MISSING_ACCEPTANCE` | ERROR |
| source 且引用空 | `MISSING_EVIDENCE_QUOTE` | ERROR |
| source 且引用不是原文子串 | `INVALID_EVIDENCE_QUOTE` | ERROR |
| inference 且没理由 | `MISSING_INFERENCE_REASON` | ERROR |
| 有 `unresolved_ref` 且无主责 | `UNRESOLVED_REFERENCE` | ERROR |
| 否则无主责 | `MISSING_OWNER` | ERROR |
| 有干系人名单且主责不在其中 | `INVALID_PRIMARY` | ERROR |
| 次责不在名单 | `INVALID_SECONDARY` | ERROR |
| 主责又出现在次责 | `PRIMARY_IN_SECONDARY` | ERROR |
| 截止日期空 | `MISSING_DEADLINE` | ADVISORY |

关键设计：

- **`unresolved` 优先于 `MISSING_OWNER`：** `elif not primary`，不会两条一起报，分配面板更干净。
- **只有 `stakeholders` 非空才检查人名是否在名单。** 名单为空时不报 INVALID_*（否则规则没抽出任何人会全军覆没）。但空主责仍报 MISSING_OWNER。
- `valid = not has_errors`，advisory 不影响 `valid`。

返回新的 `ValidationResult`，**不会**修改传入的 draft。

---

### 分：`repair_item(item, issue, source_text)`

深拷贝后再改，原 item 不变。按 `issue.code`：

1. `MISSING_ACCEPTANCE` → `完成「标题」并经过干系人确认`。
2. `MISSING_EVIDENCE_QUOTE` 且有原文 → 原文按句号拆开取第一句，最多 120 字，并改回 `SOURCE`。
3. `INVALID_EVIDENCE_QUOTE` → 标题若在原文里就用标题当引用；否则改成 `INFERENCE`，理由默认「根据会议上下文推断」，引用清空。
4. **owner 五码：直接 return 未改的拷贝。** 这是「禁止待指定」的代码落点。
5. `MISSING_TITLE` → 描述前 40 字，再不行叫「待补充任务」。

其它未知码：原样返回拷贝。

---

### 分：`apply_repairs(draft, validation, source_text)`

遍历 **ERROR**（不管 advisory）：

- `EMPTY_TASK_LIST`：continue（空清单合法，不重新分解）。
- owner 码：continue。
- 有 `item_id` 且能找到该项：换成 `repair_item` 的结果。

同一 item 可能有多条 issue，按 issues 顺序依次修。`version + 1`。返回新 `TaskDraft`。

---

## 8. `backend/app/agent/state.py`

**总：** LangGraph 的节点只能读写一个共享 state（这里是 TypedDict）。业务层用 Pydantic 快照。本文件做双向翻译。

---

### 分：`_replace(_left, right)`

```python
def _replace(_left: Any, right: Any) -> Any:
    return right
```

LangGraph 对 list 默认 **append 归约**。`pending_events` 若走默认，每个节点的事件会叠在上一节点后面，SSE 会重复刷旧进度。标成 `Annotated[list, _replace]` 后，后写的完全覆盖。

---

### 分：`AgentState`

`TypedDict, total=False`：所有键可选，节点可以只返回「本站改过的字段」，图会 merge。

比快照多出来的运行期字段：

- `clarify_response` / `review_action` / `assign_action`：HITL 醒来后的路由意图。
- `pending_events`：本节点要广播的 SSE 原料。
- `repair_attempts` / `assign_attempts`：循环上限计数。

值为 dict 的 `meeting_brief` / `task_draft` / `validation` / `review`：因为图要序列化进 checkpointer，Pydantic 对象不直接进图。

---

### 分：`snapshot_to_state(snapshot, **overrides)`

把快照 dump 成 json-friendly dict，再 `state.update(overrides)`。`run_chat` 开新跑时会 override：`source_text`、`run_id`、`pending_events=[]`、清空 interrupt 等。  
`assign_attempts` 在这里被重置为 0——新跑从零计分配次数；全文修订回 intake 时 `review_node` 也会把 `assign_attempts` 写成 0。

---

### 分：`state_to_snapshot(state, base=None)`

有 `base` 就深拷贝再覆盖（保留 created_at 等图里没的字段）。各块用 `model_validate` 从 dict 还原。  
`interrupt_payload` 用 `"interrupt_payload" in state` 判断：显式写成 `None` 表示「暂停结束了」，和「字段缺失沿用旧值」区分开。HITL 节点返回 `"interrupt_payload": None` 就是这个意思——真正展示给前端的 payload 由 `workflow._stream_graph` 从 graph_state.tasks 里取出后再写回快照。

---

## 9. `backend/app/agent/graph.py`

**总：** 把 7 个节点函数焊成一张可执行图。测试断言的拓扑常量也在这里，防止文档/代码分叉。

---

### 分：`WORKFLOW_NODES` / `WORKFLOW_EDGES`

静态清单。`test_graph_topology` 要求：

- 这些节点都在编译图里。
- 这些边都在。
- **不能有** `retrieve_playbooks`（已删除的假 RAG）。

`WORKFLOW_EDGES` 含条件边的**所有可能**终点，不是「每次运行都会走」。

---

### 分：`build_graph(checkpointer=None)`

```python
builder = StateGraph(AgentState)
builder.add_node("intake", intake_node)
...
builder.add_edge(START, "intake")
builder.add_conditional_edges("intake", route_after_intake, ["clarify", "decompose"])
...
builder.add_edge("finalize", END)
return builder.compile(checkpointer=checkpointer or MemorySaver())
```

- `add_conditional_edges` 第三个参数是路径白名单：路由函数返回值必须是其中之一。
- 没传 checkpointer 时用 `MemorySaver()`：单测一图一 saver，互不污染。
- HTTP 运行时传入 `AsyncSqliteSaver`。

路由函数（都在 `nodes.py`）在节点 **执行完、state 已 merge 之后** 才调用。所以 `route_after_assign` 读的是 `assign_owners_node` 刚写进 state 的 `assign_action`。

---

### 分：进程单例

- `get_compiled_graph()`：没有就现编，用 `get_checkpointer()`（可能还是内存版）。
- `bind_compiled_graph` / `set_compiled_graph`：lifespan 用来换成 SQLite 版。
- `reset_compiled_graph()`：进程退出或测试重置。

`SpecOpsWorkflow.graph` 是 lazy property：先 import API 时拿临时图，lifespan 之后 `workflow.graph = graph` 覆盖。

---

### 分：`get_topology()`

重新 `build_graph(MemorySaver())`，从 `compiled.get_graph()` 抽出节点和边，丢掉 `__start__` / `__end__`。给测试用，不读进程单例，避免测试顺序干扰。

---

## 10. `backend/app/agent/nodes.py`

**总：** 约 640 行，流水线每一站的实现。读这个文件时记住两条约定：

1. 节点返回值是 **部分 state**，不是整份 AgentState。
2. `pending_events` 是给前端的进度；HITL 用 `interrupt()` 暂停，返回值要等 resume 之后才继续往下执行。

---

### 分：模块级常量与小函数

- `MAX_REPAIR = 2`：规则修复最多 2 次（循环 `range(MAX_REPAIR + 1)` 里最后一次只验证不修）。
- `MAX_ASSIGN = 3`：分配 interrupt 最多 3 次。
- `_ROUTE_LABELS`：把 `rules` / `deepseek` / `deepseek+rules` 译成中文，出现在进度文案里，前端 `parseRoute` 靠这些中文打徽章。
- `_events(*items)`：把若干 dict 收成 list。
- `_progress(node, message, status=None)`：`kind=progress`。`status` 只有需要改步进条时才传（running/done/skipped）；中间「验证第 n 次」可以不带 status，避免闪烁。
- `_tool_result(data)`：`kind=tool_result`，workflow 转 SSE。

---

### 分：`intake_node(state)`（async）

1. `brief = rule_extract_brief(source)`，`used = "rules"`。
2. 若 `llm_client.enabled`：
   - 调 `structured_extract_brief`。
   - 模型 brief **有 goal** → 整份采用模型，`used=deepseek`。
   - 模型有内容但没 goal、规则也没 goal → 逐字段：模型非空值覆盖规则，`used=deepseek+rules`。
   - 模型失败返回 None → 保持规则结果（`client.py` 已打日志）。
3. 若 state 里还有 `clarify_response` 且 brief 仍无 goal：用澄清文本当 goal（兼容「第一次请求就带了澄清」；正常 HITL 澄清走 `clarify_node` 的 interrupt 返回值）。
4. 算 `next_route`，组装 pending_events：intake 一定 `done`；走 decompose 则 clarify `skipped` + decompose `running`；否则 clarify `running`。
5. 返回里 **`assign_attempts: 0`**、**`clarify_response: None`**（用过即弃，避免下一轮误用）。

不在本函数里 `interrupt`。要不要澄清由后面的路由决定。

---

### 分：`route_after_intake(state)`

再 validate 一遍 brief（图 merge 后的）。`needs_clarification` → `"clarify"` 否则 `"decompose"`。必须是字面量，对应 `graph.py` 白名单。

---

### 分：`clarify_node(state)`（async）

```python
raw = interrupt(payload)  # 图在这里冻住
```

`payload`：`type=clarify`、`interrupt_id`、`message`、`fields=["goal"]`。前端靠 `type` 弹蓝面板。

resume 之后 `interrupt()` 返回用户数据。兼容三种 key：`clarify_response` / `feedback` / `goal`（chat 层 resume 会三个都塞上）。写入 `brief.goal`。

返回：清掉 `interrupt_payload`，状态仍 `planning`（workflow 发现 interrupt 时才会改成 awaiting；本节点是「醒来之后」才返回，所以是继续跑）。进度：clarify `done` + decompose `running`。

**注意：** 第一次执行碰到 `interrupt()` 时，函数后半不会跑；workflow 从 graph_state.tasks 拿 payload 发给前端。

---

### 分：`decompose_node(state)`（async）

1. 规则 `rule_decompose`。
2. LLM 若返回 `items` 非空：`TaskDraft(items=llm_items.items, version=draft.version)` **整表替换**，不和规则结果逐条 merge。
3. 进度：`得到 N 项 · 规则回退/DeepSeek`，下一站 validate `running`。

模型返回空列表时保留规则结果（`if llm_items and llm_items.items`）。

---

### 分：`validate_repair_node(state)`（async）

```python
if llm_client.enabled:
    draft, validation, events = await _llm_tool_validate_repair(...)
else:
    draft, validation, events = _rule_validate_repair(...)
go_assign = has_owner_errors(validation)
```

结尾：自己 `done`；要分配则 assign `running`；否则 assign `skipped` + review `running`。

把 `repair_attempts` 写回 state（LLM 路径是 rounds 数）。

---

### 分：`_rule_validate_repair`

```python
for attempt in range(MAX_REPAIR + 1):  # 0,1,2
    validation = validate_task_draft_tool(...)
    repairable = [i for i in validation.error_issues if i.code not in OWNER_ERROR_CODES]
    if not repairable: break
    if attempt >= MAX_REPAIR: break
    draft = repair_task_draft_tool(...)
# 循环后再强制 validate 一次
```

- attempt=0：先验。
- 有可修 ERROR 且还没到 2：修。
- owner 错误不算 repairable，循环会 break，错误留着给分配节点。
- 循环变量里的 `validation.repair_attempts = attempt` 会在最后一次最终 validate 时被覆盖为循环内记录的值。

---

### 分：`_draft_loop_summary`

给 LLM 工具循环看的短 JSON：每条只留 id/title/主次责/代词/引用前 80 字 + 全部 issues。避免把整份草案塞进上下文。

---

### 分：`_execute_loop_tool(name, arguments, draft, brief, source)`

三个名字：

- `validate_task_draft`：不改 draft，返回 valid/error_count/codes。
- `repair_task_draft`：先 validate 再 repair（工具本身无参数）。
- `drop_items`：读 `arguments["item_ids"]`，允许传成单个值，转成 str 列表。

未知名字 `raise ValueError`，被循环里的 `except` 记为一次失败。

---

### 分：`_llm_tool_validate_repair`（async）

这是有 Key 时最复杂的一段。逐步：

1. 先本地 validate 一次，把结果当 user 消息发给模型（system = `TOOL_LOOP_SYSTEM`）。
2. `while rounds < max_tool_rounds`：
   - `chat_tools`；失败则 `consecutive_failures++`，满 `max_fail` 退出。
   - 把 assistant 消息 append 进 messages（含 tool_calls）。
   - **没有 tool_calls：** 模型认为没事了，break。
   - 对每个 tool_call：`json.loads` 参数（坏 JSON 当 `{}`）→ `_execute_loop_tool` → 成功则把 payload 以 `role=tool` 回写；失败同样回写 `{"error": ...}`。
   - 再本地 validate。若 error 数量 **没有下降**，`stall++`，满 `max_stall` 退出；下降则 stall 清零。
   - `executed==0`（这一轮一个工具都没成功执行）也退出。
3. 循环后再最终 validate，`repair_attempts = rounds`。

为什么 stall 按 error **数量**而不是码集合？简单、可实现；副作用：删掉 1 条又新冒 1 条 error 会被当成无进展。对「反复 repair 修不动 INVALID_PRIMARY」正好能停（repair 不改 owner，error 数不变）。

`drop_items` 只在这条路径出现。规则路径没有删项能力。

---

### 分：`route_after_validate_repair`

`has_owner_errors` → `assign_owners` 否则 `review`。空清单（EMPTY_TASK_LIST）不是 owner 码，会直接进 review，让人看到「没任务」再决定全文重跑。

---

### 分：`_owner_error_items(draft, validation)`

把 owner ERROR 按 `item_id` 聚合成前端分配列表：标题、描述、当前主次责、引用、代词、该条所有 owner issues。同一 item 多条码只出现一次，issues 数组里追加。没有 `item_id` 的码（理论上 owner 码都有）被跳过。

---

### 分：`assign_owners_node(state)`（async）

1. 读当前 `assign_attempts`（从 0 起）。
2. 组 payload：`type=assign_owners`、干系人、pending items、`attempt=attempts+1`、`max_attempts=3`。
3. `interrupt(payload)` 暂停。
4. 醒来后取 `raw["assignments"]`，`apply_owner_assignments`，再 validate。
5. `attempts += 1`。
6. **仍有 owner 错且 attempts &lt; 3：**  
   `assign_action = {"route": "assign_owners"}`，只发一条「分配仍不完整」进度（**不**带 done，因为还要再暂停）。  
7. **否则：** route=review，assign `done` + review `running`。满 3 次文案改为「携带未解决问题进入审核」。

`interrupt_id` 每次进入节点都 `uuid4()` 新生成，前端以最新为准。

---

### 分：`route_after_assign`

只看 `assign_action["route"]`，缺省 `"review"`。不要在路由函数里再 validate 一遍，以免和节点计数不一致。

---

### 分：`review_node(state)`（async）

payload 带全部任务摘要 + 未解决 ERROR + advisory，前端审核面板要展示。

`interrupt` 返回值若不是 dict，当成布尔批准（防御测试里传 `True`）。

四个出口：

| 条件 | `review_action.route` | 副作用 |
| --- | --- | --- |
| `approve` | `finalize` | review done + finalize running |
| `revision_scope=="document"` | `intake` | 原文末尾追加 `[文档修订] 反馈`，`assign_attempts=0` |
| 没有 `target_item_id` | `review` | 再审，避免单项修订丢目标 |
| 否则单项 | `validate_repair` | `patch_target_item`，`assign_attempts=0` |

全文修订从 intake 重跑，所以可能再次澄清/分配。单项修订只重验，不重拆（描述里多了一段反馈，标题和证据还在）。

---

### 分：`route_after_review`

白名单四选一，非法值当 `finalize`（宁可导出也不死循环）。

---

### 分：`finalize_node(state)`

`export_markdown_tool` → `markdown_output`。`review.approved=True`。`status=completed`。`review_action=None`。进度：tool_result 字数 + finalize done。

**不检查** owner ERROR：人已经在 review 点了批准。

---

## 11. `backend/app/agent/tools.py`

**总：** 四个纯函数，给节点和 LLM 工具循环调用。文件头注释写明：没有 LLM，证据硬检查留在 validation。

### 分：`validate_task_draft_tool`

直接转调 `validate_task_draft`。单独包一层是为了：节点/LLM 循环只依赖 `tools.py`，以后若加日志/埋点只改这里。

### 分：`repair_task_draft_tool`

转调 `apply_repairs`。`brief` 参数故意不用（`_ = brief`）：LLM schema 里工具无参，但调用方可把 brief 传进来方便以后扩展「按干系人修」。注释强调 **EMPTY_TASK_LIST 不要重新 decompose**——重新拆会让 `drop_items` 删光后再被规则填回来，工具循环就没意义了。

### 分：`drop_items_tool(draft, item_ids)`

`drop = {str(i) for i in item_ids if i}`，过滤掉空 id。留下不在集合里的项。version+1。允许删光（空列表合法）。

### 分：`export_markdown_tool`

转调 `render_markdown`。finalize 和「导出时还没有 markdown_output」的 API 共用同一套排版。

---

## 12. `backend/app/agent/events.py`

**总：** 把 Python dict 编码成 SSE 文本帧。workflow 和 API 幂等回放都用这些函数，保证事件名拼写一致。

### 分：`ProgressStatus`

`Literal["running", "done", "skipped", "waiting"]`。前端步进条只认这四个 + 本地的 `pending`。

### 分：`SSE_HEADERS`

- `Cache-Control: no-cache, no-transform`：禁止中间层改写流。
- `Connection: keep-alive`
- `X-Accel-Buffering: no`：告诉 Nginx 不要攒满再发给浏览器，否则进度条会卡住然后一次性跳完。

### 分：`_sse(event, data)`

```python
return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
```

`ensure_ascii=False` 让中文在日志和前端直接可读。帧以空行结束，符合 SSE 规范；前端 `split('\n\n')`。

### 分：各 `emit_*`

| 函数 | 事件名 | 典型 payload |
| --- | --- | --- |
| `emit_metadata` | metadata | run_id, thread_id, request_id, 有时 idempotent |
| `emit_progress` | progress | node, message, 可选 status |
| `emit_tool_result` | tool_result | tool 名和统计 |
| `emit_interrupt` | interrupt | HITL payload（含 type） |
| `emit_token` | token | 预留流式打字，当前节点未用 |
| `emit_result` | result | 整份 ThreadSnapshot JSON |
| `emit_error` | error | `{message}` |
| `emit_done` | done | `{}`，前端 `loading=false` |

`emit_progress` 只有 status 属于那四个字面量才写入（workflow `_pending_to_sse` 会再滤一次）。

---

## 13. `backend/app/agent/prompts.py`

**总：** 所有模型提示词和工具 schema。改 Prompt 只改这文件，不要在 `nodes.py` 里拼长字符串。

结构约定（注释里写了）：System = 角色 → 技能 → 规则 → 工作流程 → 初始化；User 模板只填运行时数据。

### 分：`INTAKE_SYSTEM` / `INTAKE_USER`

解析专员。硬规则：缺字段留空、禁止编造、只输出 `{"brief":{...}}`。User：`会议纪要：\n{source_text}`。`client.py` 会把原文截到 4000 字。

### 分：`DECOMPOSE_SYSTEM` / `DECOMPOSE_USER`

拆解专员。最重要的规则：

- `evidence_quote` 必须是原文连续子串。
- 不能填「待指定」。
- 代词无法锚定 → 空主责 + `unresolved_ref`。
- 主题/寒暄不是任务。
- 主责不得出现在次责。

User 同时给 Brief JSON 和原文，模型才能把人名对齐到 stakeholders。

### 分：`TOOL_LOOP_SYSTEM`

调度员：只能调三个工具；owner/代词问题不要猜；同一工具修不动就停。

### 分：`TOOL_SCHEMAS`

OpenAI 兼容的 `tools` 数组。`validate` / `repair` 无参数（`additionalProperties: False` 的空 object）。`drop_items` 要求 `item_ids: string[]`。这个列表原样塞进 `chat_tools` 的 POST body。

---

## 14. `backend/app/agent/workflow.py`

**总：** HTTP 与 LangGraph 之间的适配器。API 只认 `run_chat` / `resume` 两个异步生成器，每个 `yield` 都是一帧 SSE 字符串。

---

### 分：`SpecOpsWorkflow.__init__` 与 `graph` property

可注入 `graph`（测试传 `build_graph(MemorySaver())`；lifespan 传 SQLite 编译图）。若为 None，第一次访问再 `get_compiled_graph()`，这样 import API 时即使图还没 bind 也不会立刻炸。

`_config(graph_ns)` → `{"configurable": {"thread_id": graph_ns}}`。LangGraph 用这个 key 存档。**不是**业务 `thread_id`。业务线程可以多次 run，每次新 `graph_ns = thread_id:run_id`；澄清 resume 除外。

---

### 分：`run_chat(snapshot, source_text, request_id, clarify_response=None)`

逐步：

1. 新 `run_id`，快照改 planning，写入 `source_text`。
2. `resuming_clarify = bool(clarify_response and snapshot.graph_ns and snapshot.interrupt_id)`  
   - True：沿用 `graph_ns`，`Command(resume={clarify_response, feedback, goal})`。三个 key 是为了 `clarify_node` 怎么读都能中。  
   - False：`graph_ns = f"{thread_id}:{run_id}"`，全新 initial state。
3. `save_thread`。
4. 先 `yield emit_metadata`（前端立刻能显示 thread_id）。
5. `_stream_graph(...)`；异常则 error + done（**必须 done**，否则前端 `loading` 一直转）。

澄清为什么走 `run_chat` 不走 `resume`？因为前端澄清按钮调的是 `/chat/stream` 并带 `clarify_response`（产品设计：澄清更像「同一段对话的下一句」而不是「审核操作」）。后端用 interrupt 机制，所以内部仍然是 Command(resume)。

---

### 分：`resume(...)`

给分配/审核。总是 `Command(resume=payload)`，payload 同时含 `approve`、`revision_scope`、`target_item_id`、`feedback`、`assignments`。  
`assign_owners_node` 只看 assignments；`review_node` 看 approve 等。多出来的键对方忽略。

若快照没有 `graph_ns`（异常数据），会现编一个——这种 resume 通常找不到档，可能报错进 except。

---

### 分：`_stream_graph`（核心）

```python
if not isinstance(input_data, ResumeCommand):
    yield emit_progress("intake", "解析会议输入", status="running")

async for chunk in self.graph.astream(input_data, config, stream_mode="updates"):
    for _node_name, update in chunk.items():
        last_state.update(update)
        for ev in update.get("pending_events") or []:
            yield self._pending_to_sse(ev)
```

`stream_mode="updates"`：每个 chunk 是 `{节点名: 该节点返回的部分 state}`。节点结束后才来，所以 intake 的 running 必须在循环前由 workflow 发。

循环后：

1. `aget_state(config)` 拿完整图状态（含 interrupt 任务）。
2. 用 `last_state` 覆盖 values（pending_events 除外，避免把 list 写进快照）。
3. `_extract_interrupts`：
   - **有：** status=AWAITING_REVIEW，把 payload 写入快照，`complete_request` 记幂等，再发 waiting + interrupt + result + done，**return**（不要当完成）。
   - **无：** 若已有 markdown 却忘了改 status，补成 COMPLETED；保存；complete_request；result + done。

`complete_request` 在 interrupt 和完成两条路径都会调用：同一 `request_id` 重试时回放的是「这一次 stream 结束时」的快照（可能仍在等待审核）。

---

### 分：`_pending_to_sse`

只认识 `progress` / `tool_result`。其它 kind 丢弃。progress 的 status 不在四字面量里则当没有 status（时间线仍有文案，步进条不变）。

---

### 分：`_extract_interrupts(graph_state)`

两处搜集：

1. `graph_state.tasks[*].interrupts[*].value`
2. `graph_state.interrupts[*]`（有的版本挂在顶层）

value 是 dict 就原样用；否则包成 `{"message": str(val)}`。workflow 只取 `interrupts[0]`：同一时刻只展示一种 HITL。

---

## 15. `backend/app/api/schemas.py`

**总：** 请求体校验。FastAPI 用这些模型解析 JSON，失败自动 422。

### 分：`ChatStreamRequest`

- `source_text: str = Field(min_length=1)`：空字符串 422。
- `thread_id` 可选：第一次分析不传，服务端建新线程。
- `request_id` 必填且非空：幂等键。前端每次点击 `uuid()`。
- `clarify_response` 可选。

### 分：`OwnerAssignment`

`item_id` 必填；主责默认 `""`；次责默认 `[]`。允许提交「还没选主责」的行（后端会再 interrupt）。

### 分：`ResumeRequest`

字段含修订范围、目标 id、反馈、是否批准、assignments、`interrupt_type`。

`check_item_scope`（`mode="after"` 模型级校验）：

```python
if self.assignments:
    return self  # 分配请求不要求 target_item_id
if self.revision_scope == "item" and not self.approve and not self.target_item_id:
    raise ValueError("target_item_id is required when revision_scope is item")
```

点「提交修订」却没选哪一条 → 422，不会进图。点批准或带了 assignments 则放行。

---

## 16. `backend/app/api/chat.py`

**总：** `POST /api/v1/chat/stream`。开始分析或提交澄清。返回 SSE。

模块级 `workflow = SpecOpsWorkflow(memory_store)` 会被 lifespan 替换。测试则 monkeypatch 成带 MemorySaver 的实例。

### 分：`_stream_events`

透明转发异步生成器。单独函数只是为了 `StreamingResponse` 写着好看。

### 分：`chat_stream(body)` 逐步

1. **忙碌线程选择：**  
   `busy_thread = None if body.clarify_response else body.thread_id`  
   澄清时不让 `claim_request` 因 `awaiting_review` 抛忙碌。
2. `claim_request(request_id, busy_thread)`：
   - 抛 `ThreadBusyError` → 409。
   - `claimed=False` 且有 existing → 幂等短流：metadata（带 idempotent）+ result + done，**不再跑图**。
3. 若客户端传了 `thread_id`：
   - 线程 `planning` → 409（正在跑）。
   - `awaiting_review` 且本次不是澄清 → 409（应走 resume）。
4. 没传 thread_id：`ensure_thread(None)` 新建 UUID。
5. `ensure_thread(thread_id)` 取出/创建快照。
6. `StreamingResponse(workflow.run_chat(...), media_type="text/event-stream", headers=SSE_HEADERS)`。

---

## 17. `backend/app/api/review.py`

**总：** `POST /api/v1/runs/{run_id}/resume`。分配、修订、批准。

### 分：`resume_run` 逐步

1. `claim_request(body.request_id)` **不传 thread_id**：此时线程一定是 awaiting_review，若传入会立刻 409。幂等只按 request_id。
2. `find_by_run_id(run_id)`：404 if missing。路径上的 run_id 是**暂停那一次**的，前端从 snapshot.run_id 取。resume 内部会再生成新 run_id 写进快照，但 checkpointer 仍用旧 `graph_ns`。
3. 状态不是 `AWAITING_REVIEW` → 400。
4. 若 body 带了 `interrupt_type`，必须等于 `snapshot.interrupt_payload["type"]`。防止审核面板的批准打到「正在分配」的 run 上。
5. `assignments` dump 成 dict 列表。
6. `workflow.resume(...)` 流式返回。

---

## 18. `backend/app/api/threads.py`

**总：** 只读。不跑图。

### 分：`get_thread`

`get_thread` 没有则 404。返回类型标注 `ThreadSnapshot`，FastAPI 自动 JSON。

### 分：`export_thread`

`format` Query 正则 `^(md|json)$`，非法 422。

- json：整份快照（含原文、校验、interrupt 等，比 Markdown 完整）。
- md：优先 `snapshot.markdown_output`；空则 `render_markdown(...)` 现场拼（审核前也能下载当前草案）。`PlainTextResponse` + `text/markdown`。

---

## 19. `backend/app/memory/store.py`

**总：** 业务 SQLite。两张表，异步 `aiosqlite`。全局单例 `memory_store`。

路径默认 `settings.database_path`；测试传入 `tmp_path / "xxx.db"`，互不干扰。

---

### 分：`init`

```sql
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_requests (
    request_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

快照是一整份 JSON 文本，没有按字段拆列——演示项目改 schema 成本更低。`status` 列冗余一份，方便 `claim_request` 只 SELECT status 判断忙碌，不必反序列化。

---

### 分：`get_thread` / `save_thread`

get：没有行返回 None；有则 `ThreadSnapshot.model_validate_json`。  
save：补 created_at / updated_at（UTC ISO）。`INSERT ... ON CONFLICT DO UPDATE` 更新 status、json、updated_at。

---

### 分：`claim_request(request_id, thread_id=None)`

**必须在同一连接里先查 request 再查线程**，减少竞态（演示级，不是严格串行化隔离）。

1. `processed_requests` 已有该 request_id → `(False, 当时快照)`。调用方回放，不再执行。
2. 若传了 `thread_id`，且该线程 status ∈ {planning, awaiting_review} → `ThreadBusyError`。
3. 否则 `(True, None)`，允许继续。  
   **注意：** claim 成功并不插入 processed_requests。插入发生在 stream **结束**时的 `complete_request`。若进程在跑图中途崩溃，request_id 未记录，客户端用同一 id 重试会再跑一遍——这是取舍。

---

### 分：`find_by_run_id`

全表扫描 threads，反序列化后比 `snap.run_id`。演示数据量可接受。生产应加索引列。

---

### 分：`complete_request`

`INSERT OR IGNORE`：第一次写入的快照胜出。与 claim 的「已存在则回放」闭合。

---

### 分：`ensure_thread`

`thread_id or uuid4()`。已存在返回旧快照（**不会**清空）。不存在则创建 idle 空快照并 save。chat API 靠它「有则沿用、无则建」。

---

## 20. `backend/app/memory/checkpoint.py`

**总：** LangGraph 存档的工厂，和 MemoryStore **共存、不混用**。Store 给 REST/前端；这里给 `interrupt()` 恢复执行栈。

### 分：模块全局

`_checkpointer`：saver 实例。`_checkpointer_cm`：async context manager（SQLite 需要 aenter/aexit）。

### 分：`setup_checkpointer`

已有则直接返回（lifespan 与测试重复调用安全）。  
`checkpoint_path` 非空且不是 `:memory:` 时：

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
_checkpointer_cm = AsyncSqliteSaver.from_conn_string(db_path)
saver = await _checkpointer_cm.__aenter__()
await saver.setup()
```

任何异常：打 exception 日志，回退 `MemorySaver()`（进程内有效，重启丢失，HITL 刷新页面可能续不上）。

### 分：`close_checkpointer`

若有 cm，`__aexit__` 关连接，再把两个全局置 None。

### 分：`get_checkpointer` / `reset_checkpointer`

get：还没 setup 时返回**新的** MemorySaver()——注意每次调用都可能是不同对象，所以 HTTP 路径一定要走 lifespan bind。  
reset：测试用，不关连接（测试多用内存）。

---

## 21. `backend/app/llm/errors.py`

**总：** LLM 失败的类型体系。节点里 `except LLMError` 能一把抓住所有子类，日志用 `exc.code` 区分。

| 类 | code | 何时 |
| --- | --- | --- |
| `LLMError` | `LLM_ERROR` | 基类 |
| `LLMConfigError` | `LLM_CONFIG` | provider 不是 deepseek，或 Key 空却调了 post |
| `LLMTimeoutError` | `LLM_TIMEOUT` | wait_for 或 httpx 超时 |
| `LLMAPIError` | `LLM_API` | 传输错误或 HTTP ≥ 400，可带 `status_code` |
| `LLMParseError` | `LLM_PARSE` | 响应非 JSON、缺 choices、content 非 JSON、Pydantic 失败 |

`structured_*` 捕获这些后返回 None，节点回退规则。工具循环里捕获后算一次 consecutive_failure，不整段回退规则（已经在 LLM 模式里了，停循环即可）。

---

## 22. `backend/app/llm/client.py`

**总：** DeepSeek Chat Completions 的唯一出口。节点禁止直接 httpx。

全局 `llm_client = LLMClient()`。评测和 `enabled` 检查的是这个单例（测试会 monkeypatch `nodes.llm_client` 或替换模块内单例）。

---

### 分：`enabled`

```python
return self.provider not in ("none", "") and bool(settings.deepseek_api_key)
```

只改 `LLM_PROVIDER=deepseek` 但 Key 为空 → 仍视为关闭，走规则。CI 双保险。

`__init__` 里 `self.provider = settings.llm_provider.lower()`：**构造时拍快照**。测试必须 `LLMClient()` 新实例，或同时 patch settings 再 new。

---

### 分：`_completions_url` / `_headers`

URL = `base_url.rstrip('/') + '/chat/completions'`。Header 只有 Bearer。不在日志里打印 Key。

---

### 分：`_with_thinking_disabled`

```python
payload.setdefault("thinking", {"type": "disabled"})
```

DeepSeek V4 Flash 默认 thinking=high，思维链 token 可能远超 30s 读超时。`setdefault` 允许测试传入已有 thinking 配置时不覆盖。`test_post_completions_disables_thinking` 断言发出去的 JSON 含这一项。

---

### 分：`post_completions(payload, node)`（async）

1. provider 必须是 `deepseek`，否则 `LLMConfigError`。
2. Key 空同样 ConfigError（enabled 为 False 时节点不该调到这里；工具循环在 enabled 为 True 时才会来）。
3. `asyncio.wait_for(client.post(...), timeout=timeout_s)` 套在 httpx timeout 外面：httpx 读超时在 chunk 之间会重置，模型若持续慢慢吐 thinking，墙钟时间仍可能超过 30s 却不触发 httpx 超时。
4. `TimeoutError` 与 `httpx.TimeoutException` 都变 `LLMTimeoutError`，消息带 node 名，方便日志。
5. 其它 HTTPError → `LLMAPIError`。
6. status ≥ 400 → APIError，body 只切 300 字，避免把整页 HTML 打进日志。
7. `resp.json()` 失败 → ParseError。

`node` 只用于错误文案（intake/decompose/validate_repair）。

---

### 分：`structured_extract_brief` / `structured_decompose`

`enabled` 假则立刻 None。否则 `chat_json`，失败 `logger.warning` 后 None。原文/brief 截断 4000 字，控制费用和超时。

---

### 分：`chat_json(..., model_cls, node)`

POST body：

- `response_format: {type: json_object}` 要求模型吐 JSON。
- `temperature: 0.1` 偏确定性。
- thinking disabled。

取出 `choices[0].message.content` → `json.loads` → `model_cls.model_validate`。任一步失败变 ParseError。校验失败只把 `exc.errors()[:3]` 放进消息，避免巨大 schema 报错。

---

### 分：`chat_tools(messages, node)`

POST 带 `tools=TOOL_SCHEMAS`、`tool_choice=auto`。返回 **assistant message 整个 dict**（可能含 `content` 和 `tool_calls`），不做 Pydantic。形状不对抛 ParseError。`nodes.py` 负责执行 tool_calls。

---

## 23. `backend/evals/generate_cases.py`

**总：** 黄金集的**源代码**。改评测期望应改 `CASES` 再 `python -m evals.generate_cases`，不要手改由它生成的 json。

运行：`if __name__ == "__main__": write_golden()`。pytest 里 `test_eval_golden` 也会先调 `write_golden()`，保证仓库 json 与模板同步。

---

### 分：`CASES` 结构

每条：

```python
{
  "id": "single_01",
  "source_text": "...",
  "expect": {
      "needs_clarify": False,
      "needs_assignment": False,
      "items": [{"title_keywords": ["认证"], "primary_owner": "张伟", "secondary_owners": []}],
  },
  "gold": {"clarify": "", "assignments": []},
}
```

- `expect`：打分用（抽没抽到、HITL 该不该出现）。
- `gold`：runner 在 interrupt 时注入的「假用户输入」。`assignments` 里仍用 `title_keywords` 匹配真实 item id（id 每次 UUID 不同）。

类型：

| id 前缀 | 意图 |
| --- | --- |
| `single_*` | 一人一事，通畅到 review |
| `multi_*` | 主次责拆分 |
| `assign_*` | 原文没点名，要分配 |
| `clarify_*` | 没会议目标 |
| `clarify_assign_01` | 先澄清再分配 |

**`assign_04` 不在此列表。** 手写 json 测代词。`write_golden` 不会覆盖它（只写 CASES 里的 id），也不会删除多余文件。

---

### 分：`write_golden`

确保 `golden/` 目录存在，对每条 case `json.dumps(..., ensure_ascii=False, indent=2)` 写 `{id}.json`。返回写成的路径列表。

---

## 24. `backend/evals/runner.py`

**总：** 在隔离环境里把一个黄金 case 从 intake 跑到有 Markdown（或失败）。强制 `llm_client.enabled is False`。

---

### 分：常量

`MAX_STEPS = 12`：HITL 次数上限，防止死循环。  
`KNOWN_INTERRUPTS = {clarify, assign_owners, review}`：以后新加 HITL 忘了更新评测会直接 fail。

---

### 分：`load_cases`

`golden/*.json` 按文件名排序读入。包含手写的 `assign_04`。

---

### 分：`_keyword_score` / `match_item`

标题+描述里命中多少个 keyword。在未使用的 items 里选得分最高且 &gt; 0 的一条，id 放进 `used`（一对一，不重复匹配）。匹配不上返回 None → 召回失败或分配注入跳过该项。

---

### 分：`_assignments_for(snap, case)`

把 gold.assignments 的 keyword 对到当前草案真实 id，生成 `apply_owner_assignments` 要的结构。

---

### 分：`run_case(case, tmp_dir)`（async）

1. enabled 则 `EvalError`（拒绝用真模型污染冻结集）。
2. 该 case 独立 `MemoryStore(tmp_dir/{id}.db)` + `build_graph(MemorySaver())` + 自己的 `graph_ns`。
3. `run_chat` 把 SSE 丢弃（`async for _ in ...: pass`），只关心落盘快照。
4. 循环最多 12 次：
   - 有 markdown 且当前不是 awaiting+interrupt → 成功返回。
   - 无 interrupt 也无 markdown → 失败。
   - 未知 type → 失败。
   - clarify：用 gold.clarify，缺省「补充会议目标」，走 `run_chat(..., clarify_response=)`。
   - assign_owners：注入匹配后的 assignments，`resume`。
   - review：批准第一条（或 None），`approve=True`。
5. 超 cap → EvalError。

返回 `{snapshot, seen, steps}`。`seen` 是 interrupt 类型列表，给 score 判断 HITL 是否该出现。

---

## 25. `backend/evals/score.py`

**总：** 把 runner 结果变成四个分数，再和写死的门槛比。

门槛：召回 0.9、主责 0.8、次责 0.8、HITL 一致率 1.0。改门槛就是改这四个常量。

### 分：`score_case`

对 expect.items 逐条 `match_item`。匹配成功：

- `primary_owner` 字符串完全相等才算主责对。
- `secondary_owners` **集合**相等（顺序无关）。

`recall = matched / len(gold_items)`（没金标则 1.0）。  
主次责准确率分母是 **配对成功数**（没配上的不算进准确率，只罚召回）。

HITL：`("clarify" in seen) == needs_clarify` 且 assign 同理。多出来一次 clarify 也会 fail。

### 分：`aggregate`

对所有 case 的四分做算术平均（每个 case 权重相同，不按任务条数加权）。四项都过线 `passed=True`。返回值含每条 row，失败时 pytest 信息能看出是哪条 id 拖后腿。

---

## 26. `backend/tests/test_workflow.py`

**总：** 仓库里最大的测试文件（约 850 行）。既测纯函数，也测整图 HITL，还用 respx mock DeepSeek。

公共夹具数据：

- `SAMPLE_TEXT`：三人三任务的标准纪要，主责都能抽到，应进 **review** 而不是 assign。
- `MULTI_OWNER_TEXT`：周婷统筹 + 三人配合。

下面按测试函数说明**在锁什么行为**。

---

### 拓扑与规则抽取

| 测试 | 锁住的行为 |
| --- | --- |
| `test_graph_topology` | 7 节点、关键边存在；**没有** `retrieve_playbooks` |
| `test_rule_extract_brief` | SAMPLE 能抽出 goal 和至少 2 个干系人 |
| `test_rule_extract_brief_speakers` | `张伟：` 发言格式进入干系人 |
| `test_rule_extract_brief_topic_header` | 对照案例1 全文：干系人名字不含「负责」、至少 20 条任务、周婷主责上线且次责三人、Markdown 按人分节且正文无通用验收套话、标题出现次数不超过主+次责次数 |
| `test_feature_review_sample_hits_unresolved_ref` | 案例3 有 `unresolved_ref=="她"` 且 has_owner_errors |
| `test_short_title_*` | 剪标题：去截止、去「前」、无标记时 50 字截断 |
| `test_generic_acceptance_skipped_in_markdown` | 套话正则识别 |
| `test_needs_clarification` | 空 goal 为 True |
| `test_rule_decompose_evidence_backed` | 每条 quote 都是原文子串 |
| `test_multi_owner_extract` | 统筹/配合拆主次 |
| `test_non_task_sentence_not_in_draft` | 「先同步一下背景」不是任务 |
| `test_pronoun_unresolved_reference_suppresses_missing_owner` | 只报 UNRESOLVED，不叠 MISSING_OWNER，主责被清空 |

---

### 校验与修补

| 测试 | 锁住的行为 |
| --- | --- |
| `test_validate_quote_substring` | 瞎编引用 → INVALID_EVIDENCE_QUOTE |
| `test_validate_invalid_primary` | 外人当主责 → INVALID_PRIMARY + has_owner_errors |
| `test_repair_does_not_fill_placeholder_owner` | 修验收，但主责仍是「外人」，绝不变「待指定」 |
| `test_patch_target_item_preserves_ids` | 只改目标描述，两条 id 都在 |
| `test_drop_items_empty_is_legal` | 删光 → EMPTY_TASK_LIST，repair 不会把任务变回来 |

---

### HTTP / 工作流集成（async，临时 SQLite + MemorySaver）

| 测试 | 锁住的行为 |
| --- | --- |
| `test_health` | `/health` 200 |
| `test_full_workflow_none_provider` | SAMPLE 走到 review interrupt；每条都有主责；有 graph_ns；SSE 含 progress/running/tool_result |
| `test_repair_exhausted_still_reviews` | 抽得很烂的短文本仍能进 awaiting_review（不卡死） |
| `test_unassigned_owner_hits_assign_interrupt` | 「认证模块这周要完成」无主责 → assign_owners |
| `test_partial_assignment_reinterrupts` | 两条待分配只填一条 → 第二次仍是 assign_owners |
| `test_assign_then_review_approve` | 填主次责后进 review，再批准 → COMPLETED 且 Markdown 有人名节 |
| `test_item_revise_and_approve` | 单项修订后再批准能完成 |
| `test_idempotency` | complete 后再 claim 同一 request → claimed False |
| `test_thread_busy` | planning 时 claim 抛 ThreadBusyError |
| `test_export_endpoint` | 渲染含「会议纪要」「主责」，不含「同步状态」 |
| `test_clarify_hitl` | 无目标先 clarify；带 clarify_response 后续 goal 非空（然后通常到 review） |
| `test_pronoun_hits_assign_interrupt` | 「交给她」整图停在 assign，payload items 带代词和引用 |

---

### LLM mock（respx，不打真实网）

| 测试 | 锁住的行为 |
| --- | --- |
| `test_llm_fallback_with_httpx_mock` | 500 错误时仍能靠规则产出任务并 interrupt |
| `test_llm_structured_success` | 按 system 文案区分 intake/decompose 返回不同 JSON；证据仍是原文子串 |
| `test_llm_tool_loop_validate_then_drop` | 模型先 validate 再 drop `junk-1`；最终草案不含垃圾项 |
| `test_llm_tool_loop_stall_stops` | 主责是外人，模型反复 repair（修不动 owner）；调用次数在 2～4 之间然后停，进入 HITL |

`_enable_llm`：patch settings + 把新 `LLMClient` 接到 `app.agent.nodes.llm_client`。必须 patch nodes 上的引用，因为 nodes 是 `from app.llm.client import llm_client` 的名字绑定。

---

## 27. `backend/tests/test_api.py`

**总：** 不经过真实 TCP，用 `httpx.ASGITransport(app=app)` 打 FastAPI。验证路由、状态码、SSE 头、resume 批准。

### 分：`api_client` fixture

建临时 DB + MemorySaver 图，monkeypatch `memory_store` 和 chat/review/threads 三个模块里的 store/workflow。yield `(client, store)`。这样测试互不影响，也不碰开发者本地 `specops.db`。

### 分：各测试

- `test_chat_stream_returns_sse`：200、`text/event-stream`、`x-accel-buffering=no`、body 含 progress / tool_result / interrupt 或 result。
- `test_thread_busy_409`：先把线程标 awaiting_review，再 POST chat（无 clarify）→ 409 且 `detail.code==THREAD_BUSY`。
- `test_document_revise_scope`：走 workflow（不经过 HTTP）全文修订，断言 source_text 含「文档修订」且仍 awaiting（重跑后又会停在 review/clarify）。
- `test_resume_approve_via_api`：从 SSE 或 store 取出 run_id，POST resume `approve=True`，最终 COMPLETED。

---

## 28. `backend/tests/test_llm_client.py`

**总：** 只测 HTTP 客户端两件容易回归的事，不跑整图。

### 分：`test_post_completions_disables_thinking`

monkeypatch provider/key/url/model，respx 截获 POST，断言 body JSON `thinking == {type: disabled}`。防止以后有人删掉这行导致 Flash 超时。

### 分：`test_post_completions_wall_clock_timeout`

timeout 设 0.01 秒，mock 抛 `httpx.ReadTimeout`，断言变成 `LLMTimeoutError`。保证超时类型走统一错误码，节点才能 fallback/停循环。

---

## 29. `backend/tests/test_eval_golden.py`

**总：** 一条测试跑完整套黄金集。CI 的 `pytest -q` 会执行它。

逐步：

1. `write_golden()` 同步模板 json。
2. `llm_client.enabled` 则 `pytest.fail`（本地若 `.env` 配了 Key，评测拒绝运行，避免「模型抽得好/坏」污染规则回归）。
3. `load_cases()` 至少 12 条，且至少 2 条 `multi_*`。
4. 每条 `run_case`，`EvalError` 变 fail。
5. `aggregate` 必须 `passed`，失败信息带四分数字和 rows。

这条测试慢（每条 case 都编译图、写临时 DB、可能多次 HITL），但门槛是发布门禁。

---

## 附录：py 文件依赖关系（谁 import 谁）

```text
main.py
  ├─ config.settings
  ├─ memory.store / checkpoint
  ├─ agent.graph.bind_compiled_graph
  ├─ api.chat / review / threads
  └─ agent.workflow.SpecOpsWorkflow   （lifespan 里再绑）

api.chat / review
  ├─ schemas
  ├─ workflow
  ├─ memory.store
  └─ events.SSE_HEADERS

api.threads
  ├─ memory.store
  └─ extractors.render_markdown   （导出时可能）

workflow
  ├─ graph.get_compiled_graph
  ├─ state.snapshot_to_state / state_to_snapshot
  ├─ events.emit_*
  └─ memory.store.save / complete_request

graph → nodes（七个函数 + 四个 route_*）

nodes
  ├─ extractors / validation / models
  ├─ tools.*
  ├─ llm.client
  ├─ prompts.TOOL_LOOP_SYSTEM
  └─ langgraph.interrupt

tools → extractors.render_markdown, validation.*

llm.client → prompts.*, models.MeetingBriefExtract/ActionItemList, llm.errors, config

evals.runner → graph, workflow, store, llm.client.enabled
evals.score → runner.match_item
evals.generate_cases → 只写 json，不 import app（避免启动副作用）
```

读代码时如果迷路：从 **HTTP 进来的那一层** 往下追，或从 **models.ActionItem** 往上看谁在改它。
