# SpecOps Agent

SpecOps Agent 是一个面向中文会议纪要的 Spec-Driven、Human-in-the-Loop
任务规划 Agent。它把非结构化讨论转换为可追溯的任务清单，在交付前提供澄清、
校验、人工修订和审批，并支持 Markdown 与 JSON 导出。

## 核心能力

- **证据可追溯**：每个任务保留原文证据，校验 `evidence_quote` 必须来自输入。
- **状态图编排**：使用 LangGraph 串联解析、澄清、拆解、知识检索、修复和审核。
- **Human-in-the-Loop**：通过 `interrupt` 与 `Command(resume=...)` 暂停并恢复工作流。
- **实时过程反馈**：FastAPI 通过 SSE 推送模型、工具、审核和结果事件。
- **可选 LLM**：支持 DeepSeek 结构化输出；无 API Key 时自动走确定性规则路径。
- **可靠执行**：提供请求幂等、线程忙碌保护、SQLite 快照和 LangGraph checkpoint。
- **轻量知识工具**：本地 Markdown Playbook 采用关键词检索，不依赖向量数据库。

## 工作流

```text
intake (DeepSeek | rules)
  → clarify? (HITL interrupt)
  → decompose (DeepSeek | rules)
  → retrieve_playbooks (tool)
  → validate_repair (tool loop ≤ 2)
  → review? (HITL interrupt)
  → finalize (Markdown / JSON)
```

```text
Vue 3 UI ──SSE/HTTP──> FastAPI API ──> SpecOpsWorkflow
                                        ├─ LangGraph + checkpoint
                                        ├─ DeepSeek / rules
                                        ├─ Playbook / validation tools
                                        └─ SQLite snapshots / idempotency
```

## 技术栈

| 部分 | 技术 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Vite |
| API | FastAPI、Pydantic、Uvicorn |
| Agent | LangGraph、DeepSeek API、规则回退 |
| 存储 | aiosqlite、LangGraph SQLite Checkpointer |
| 测试 | Pytest、pytest-asyncio、respx、Ruff |
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

没有 DeepSeek Key 时，将 `LLM_PROVIDER` 设为 `none`。模型节点会使用规则抽取器，
适合本地演示和 CI。不要提交 `.env`、API Key、数据库或运行日志。

## Demo 使用

前端内置三类会议纪要：

- **冲刺计划**：包含明确负责人、绝对截止时间、周期任务和异常响应任务。
- **事故复盘**：演示故障整改、监控和回滚事项。
- **需求评审**：演示范围、依赖和待确认事项。

操作流程：

1. 点击示例按钮载入会议纪要，或粘贴自己的文本。
2. 点击“开始分析”，在时间线中查看模型与工具执行过程。
3. 如果缺少会议目标，根据提示补充澄清信息。
4. 在审核阶段批准结果，或按条目/全文提交修订意见。
5. 导出 Markdown 或 JSON 任务清单。

运行时样例统一存放在
[`frontend/public/samples/`](frontend/public/samples/)。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 服务与 LLM Provider 状态 |
| `POST` | `/api/v1/chat/stream` | 创建任务规划并返回 SSE 事件流 |
| `POST` | `/api/v1/runs/{run_id}/resume` | 澄清、修订或批准工作流 |
| `GET` | `/api/v1/threads/{thread_id}` | 获取线程快照 |
| `GET` | `/api/v1/threads/{thread_id}/export?format=md\|json` | 导出结果 |

SSE 事件类型包括 `metadata`、`progress`、`tool_result`、`interrupt`、
`result`、`error` 和 `done`。

## 测试与构建

后端：

```powershell
cd backend
ruff check app tests
pytest
```

前端：

```powershell
cd frontend
npm ci
npm run build
```

GitHub Actions 会使用 Python 3.12、Node.js 22 和 mock LLM 执行相同检查，
不读取真实 API Key。

## 项目结构

```text
specops-agent/
├── .github/workflows/ci.yml
├── backend/
│   ├── app/
│   │   ├── agent/          # LangGraph、节点、工具、Prompt 与 SSE
│   │   ├── api/            # Chat、审核与线程 API
│   │   ├── domain/         # 数据模型、抽取和校验
│   │   ├── knowledge/      # 本地 Playbook
│   │   ├── llm/            # DeepSeek 客户端
│   │   └── memory/         # 快照、幂等与 checkpoint
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── public/samples/     # 唯一的运行时 Demo 样例
│   └── src/                # Vue UI 与 SSE 客户端
├── docs/INTERVIEW.md
├── docker-compose.yml
└── README.md
```

## 设计边界

- Playbook Lookup 是可解释的关键词工具，不是向量 RAG。
- 外部系统同步字段为演示状态，不会真实写入企业系统。
- 当前项目未实现生产级鉴权、租户隔离、限流和密钥托管。
- Docker 前端容器用于开发演示，不是 Nginx 等生产静态部署方案。

## 安全说明

- `backend/.env` 仅供本地使用，已被 `.gitignore` 排除。
- API Key 一旦进入日志、提交历史或公开渠道，应立即在服务商控制台轮换。
- 上传前请检查 `.env`、`*.db`、`*.log`、IDE 配置和构建产物未被追踪。
