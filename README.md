# SpecOps Agent

SpecOps Agent 从中文**会议对话**中抽取可执行任务，并为每项任务分配**主要负责人**与**次要负责人**。
无法从原文确认或负责人非法时进入人工分配，而不是用规则填写「待指定」。
交付前保留澄清、校验、人工审核，并支持 Markdown / JSON 导出。

## 核心能力

- **主/次责拆分**：一人主责；多人时按「负责/统筹」与「配合/协助」拆主次。
- **证据可追溯**：每个任务保留原文证据，`evidence_quote` 必须是输入子串。
- **状态图编排**：LangGraph 串联解析、澄清、拆解、校验修复、负责人建议、负责人分配和审核。
- **Human-in-the-Loop**：缺目标澄清、非法/缺失负责人分配、最终审核，均用 `interrupt` 恢复。
- **链路可视化**：SSE `progress.status` 为 `running | done | skipped | waiting`，前端步进条高亮当前节点。
- **可选 LLM**：DeepSeek 结构化输出；无 API Key 时走确定性规则路径。负责人建议由模型提出、人工确认后才写入（建议 ≠ 写入）。
- **可靠执行**：请求幂等、线程忙碌保护、SQLite 快照和 LangGraph checkpoint。

## 工作流

```text
intake (DeepSeek | rules)
  → clarify? (HITL，缺会议目标)
  → decompose (DeepSeek | rules)
  → validate_repair (LLM 工具循环有上限 | 规则修复 ≤ 2，不修复负责人)
  → propose_owners? (有 Key 时始终生成建议；有 owner ERROR 或无 Key 失败则为空)
  → assign_owners? (HITL，任一 owner 校验 ERROR，含未消解的他/她；点选建议后须提交)
  → review? (HITL，批准 / 单项修订 / 删除 / 全文重跑；主责已合法时可点选次责建议)
  → finalize (Markdown / JSON)
```

`assign_owners` 最多 3 次。仍非法则**携带 ERROR 进入 review**，由人工显式批准；`finalize` 不再因 owner ERROR 拦截。
模型给出的负责人建议只出现在分配面板或审核面板；`confidence` 映射为低/中/高仅供参考，不自动写入主责/次责。没有协作信号时不出次责建议。JSON 导出剥离 `owner_suggestions`。

```text
Vue 3 UI ──SSE/HTTP──> FastAPI API ──> SpecOpsWorkflow
                                        ├─ LangGraph + checkpoint
                                        ├─ DeepSeek / rules
                                        ├─ validation / export tools
                                        └─ SQLite snapshots / idempotency
```

## 技术栈

| 部分 | 技术 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Vite |
| API | FastAPI、Pydantic、Uvicorn |
| Agent | LangGraph、DeepSeek API、规则回退 |
| 存储 | aiosqlite、LangGraph SQLite Checkpointer |
| 测试 | Pytest、pytest-asyncio、respx、Ruff、冻结评测集 |
| 工程化 | Docker Compose、GitHub Actions |

## 运行要求

- Python 3.11+
- Node.js 22+ 与 npm
- 可选：Docker Desktop / Docker Engine + Compose
- 可选：DeepSeek API Key

## 快速开始

### 本地运行

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[agent,dev]"
copy .env.example .env
uvicorn app.main:app --reload --port 8001
```

前端（新终端）：

```powershell
cd frontend
npm install
npm run dev
```

打开：

- Web UI：<http://localhost:5173>
- API：<http://localhost:8001>
- 健康检查：<http://localhost:8001/health>

### Docker Compose

```powershell
docker compose up --build
```

Compose 默认使用 `LLM_PROVIDER=none`，因此无需 API Key 也能演示完整流程。

## 环境配置

将 [`backend/.env.example`](backend/.env.example) 复制为 `backend/.env`：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
LLM_TIMEOUT_SECONDS=30
CORS_ORIGINS=http://localhost:5173
DATABASE_PATH=./specops.db
CHECKPOINT_PATH=./specops_checkpoints.db
```

没有 DeepSeek Key 时，将 `LLM_PROVIDER` 设为 `none`。不要提交 `.env`、API Key、数据库或运行日志。

## Demo 使用

前端内置三个会议纪要样例（按钮为案例1 / 案例2 / 案例3）：

- **案例1**：新产品上线推进，陈述句纪要，一人多任务，含主/次责与周期执行。
- **案例2**：预发事故复盘，夹杂非任务背景句，再落到可执行项。
- **案例3**：需求评审待办，出现「交给她负责」，原文无法消解指代，进入人工分配。

操作流程：

1. 点击示例按钮载入对话，或粘贴自己的文本。
2. 点击「开始分析」，在顶部链路条查看当前节点。
3. 缺会议目标时补充澄清；缺/非法负责人时在分配面板选择主责/次责。
4. 审核阶段批准，按条目修订，或删除非任务（全局序号 / 删除按钮）；也可全文重跑。
5. 导出 Markdown 或 JSON。

样例在 [`frontend/public/samples/`](frontend/public/samples/)。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 服务与 LLM Provider 状态 |
| `POST` | `/api/v1/chat/stream` | 创建任务规划并返回 SSE 事件流 |
| `POST` | `/api/v1/runs/{run_id}/resume` | 分配负责人、修订或批准 |
| `GET` | `/api/v1/threads/{thread_id}` | 获取线程快照 |
| `GET` | `/api/v1/threads/{thread_id}/export?format=md\|json` | 导出结果 |

SSE `progress` 带 `status`: `running` / `done` / `skipped` / `waiting`。
`interrupt.type` 为 `clarify` | `assign_owners` | `review`。

Resume 分配示例：`{"interrupt_type":"assign_owners","assignments":[{"item_id":"...","primary_owner":"张伟","secondary_owners":["李娜"]}]}`。
`interrupt_type` 若与当前暂停类型不符则 400。

## 测试与评测

后端：

```powershell
cd backend
ruff check app tests evals
pytest
```

冻结评测集（规则路径，**必须** `LLM_PROVIDER=none`；若检测到 LLM Key 会直接失败）：

```powershell
cd backend
python -m evals.generate_cases
pytest tests/test_eval_golden.py -q
```

先 `generate_cases` 再把 `evals/golden/*.json` 提交进仓库。门槛写死：任务召回 ≥ 0.9，主责 exact ≥ 0.8，次责 set 相等 ≥ 0.8，HITL 触发一致 = 100%。

前端：

```powershell
cd frontend
npm ci
npm run build
```

GitHub Actions 使用 Python 3.12、Node.js 22 和 `LLM_PROVIDER=none`。

## 项目结构

```text
specops-agent/
├── .github/workflows/ci.yml
├── backend/
│   ├── app/
│   │   ├── agent/          # LangGraph、节点、工具、Prompt 与 SSE
│   │   ├── api/            # Chat、审核与线程 API
│   │   ├── domain/         # 数据模型、抽取和校验
│   │   ├── llm/            # DeepSeek 客户端
│   │   └── memory/         # 快照、幂等与 checkpoint
│   ├── evals/              # 生成器、runner、score、冻结 golden
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── public/samples/
│   └── src/
├── docs/INTERVIEW.md
├── docker-compose.yml
└── README.md
```

## 设计边界

- 没有知识库检索 / 向量 RAG。
- 外部系统同步字段为演示状态，不会真实写入企业系统。
- 未实现生产级鉴权、租户隔离、限流和密钥托管。
- Docker 前端容器用于开发演示，不是 Nginx 生产静态部署。

## 安全说明

- `backend/.env` 仅供本地使用，已被 `.gitignore` 排除。
- API Key 一旦进入日志、提交历史或公开渠道，应立即在服务商控制台轮换。
- 上传前请检查 `.env`、`*.db`、`*.log`、IDE 配置和构建产物未被追踪。
