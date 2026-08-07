<script setup lang="ts">
import { ref, computed } from 'vue'
import { streamSSE, uuid } from './sse'

interface ActionItem {
  id: string
  title: string
  description: string
  owner: string
  deadline: string
  acceptance_criteria: string
  evidence_kind: string
  evidence_quote: string
  inference_reason: string
}

interface Snapshot {
  thread_id: string
  run_id: string
  status: string
  source_text: string
  meeting_brief: { goal: string; stakeholders: string[]; meeting_date: string }
  task_draft: { items: ActionItem[] }
  validation: { issues: Array<{ code: string; message: string; severity: string; item_id?: string }> }
  retrieve_results: Array<{ source: string; excerpt: string; note: string }>
  markdown_output: string
  idempotent?: boolean
}

interface ProgressEntry {
  node: string
  message: string
  kind?: string
  route?: string | null
}

const sourceText = ref('')
const progressLog = ref<ProgressEntry[]>([])
const toolLog = ref<Array<Record<string, unknown>>>([])
const retrieveResults = ref<Snapshot['retrieve_results']>([])
const snapshot = ref<Snapshot | null>(null)

const MODEL_NODES = new Set(['intake', 'decompose', 'llm_repair', 'clarify'])
const TOOL_NODES = new Set(['retrieve_playbooks', 'validate_repair', 'finalize'])

function stepKind(node: string): string {
  if (MODEL_NODES.has(node)) return 'model'
  if (TOOL_NODES.has(node) || node.startsWith('validate') || node === 'retrieve') return 'tool'
  if (node === 'review') return 'hitl'
  return 'step'
}

/** Parse DeepSeek / 规则回退 route label from progress message. */
function parseRoute(message: string): string | null {
  if (message.includes('DeepSeek + 规则') || message.includes('DeepSeek+规则') || /deepseek\+rules/i.test(message)) {
    return 'DeepSeek + 规则'
  }
  if (message.includes('DeepSeek / 规则回退')) return 'DeepSeek / 规则回退'
  if (message.includes('规则回退') || /\bvia rules\b/i.test(message) || /·\s*规则回退/.test(message)) {
    return '规则回退'
  }
  if (message.includes('DeepSeek') || /\bdeepseek\b/i.test(message)) {
    return 'DeepSeek'
  }
  return null
}

function routeBadgeClass(route: string): string {
  if (route === '规则回退') return 'route-rules'
  if (route === 'DeepSeek') return 'route-deepseek'
  if (route === 'DeepSeek + 规则') return 'route-hybrid'
  return 'route-pending'
}

function pushProgress(d: { node: string; message: string }) {
  progressLog.value.push({
    ...d,
    kind: stepKind(d.node),
    route: parseRoute(d.message),
  })
}

const interrupt = ref<Record<string, unknown> | null>(null)
const clarifyInput = ref('')
const reviseScope = ref<'item' | 'document'>('item')
const reviseItemId = ref('')
const reviseFeedback = ref('')
const showReviseForm = ref(false)
const loading = ref(false)
const errorMsg = ref('')
const threadId = ref<string | null>(null)

const demoSamples = [
  { name: '冲刺计划', path: '/samples/demo_sprint_planning.txt' },
  { name: '事故复盘', path: '/samples/demo_incident_review.txt' },
  { name: '需求评审', path: '/samples/demo_feature_review.txt' },
]

const taskItems = computed(() => snapshot.value?.task_draft?.items ?? [])
const errorIssues = computed(() =>
  snapshot.value?.validation?.issues?.filter(i => i.severity === 'error') ?? []
)
const advisoryIssues = computed(() =>
  snapshot.value?.validation?.issues?.filter(i => i.severity === 'advisory') ?? []
)
const markdownPreview = computed(() => snapshot.value?.markdown_output ?? '')

async function loadSample(path: string) {
  try {
    const resp = await fetch(path)
    sourceText.value = await resp.text()
  } catch {
    errorMsg.value = '无法加载示例文件，请手动粘贴'
  }
}

async function runWorkflow(clarify?: string) {
  loading.value = true
  errorMsg.value = ''
  progressLog.value = []
  toolLog.value = []
  retrieveResults.value = []
  interrupt.value = null
  showReviseForm.value = false

  const requestId = uuid()
  await streamSSE('/api/v1/chat/stream', {
    source_text: sourceText.value,
    thread_id: threadId.value,
    request_id: requestId,
    clarify_response: clarify || null,
  }, {
    onMetadata: (d) => { if (d.thread_id) threadId.value = d.thread_id as string },
    onProgress: (d) => pushProgress(d),
    onToolResult: (d) => {
      toolLog.value.push(d)
      if (d.results) retrieveResults.value = d.results as typeof retrieveResults.value
    },
    onInterrupt: (d) => { interrupt.value = d },
    onResult: (d) => { snapshot.value = d as unknown as Snapshot },
    onError: (d) => { errorMsg.value = d.message },
    onDone: () => { loading.value = false },
  })
  loading.value = false
}

async function resume(approve = false) {
  if (!snapshot.value?.run_id) return
  loading.value = true
  errorMsg.value = ''
  interrupt.value = null
  showReviseForm.value = false

  await streamSSE(`/api/v1/runs/${snapshot.value.run_id}/resume`, {
    request_id: uuid(),
    revision_scope: reviseScope.value,
    target_item_id: reviseScope.value === 'item' ? reviseItemId.value : null,
    feedback: reviseFeedback.value,
    approve,
  }, {
    onProgress: (d) => pushProgress(d),
    onToolResult: (d) => {
      toolLog.value.push(d)
      if (d.results) retrieveResults.value = d.results as typeof retrieveResults.value
    },
    onInterrupt: (d) => { interrupt.value = d },
    onResult: (d) => { snapshot.value = d as unknown as Snapshot },
    onError: (d) => { errorMsg.value = d.message },
    onDone: () => { loading.value = false },
  })
  loading.value = false
}

function download(format: 'md' | 'json') {
  if (!threadId.value) return
  window.open(`/api/v1/threads/${threadId.value}/export?format=${format}`, '_blank')
}

function selectItemForRevise(id: string) {
  reviseItemId.value = id
  reviseScope.value = 'item'
  showReviseForm.value = true
}
</script>

<template>
  <div class="app">
    <header class="header">
      <div class="header-inner">
        <h1>SpecOps Agent</h1>
        <p class="subtitle">会议纪要 → 结构化任务 → 人工审核 → 导出</p>
      </div>
    </header>

    <main class="main grid-2">
      <section class="col-input">
        <div class="card">
          <h2>会议纪输入</h2>
          <div class="sample-row">
            <button v-for="s in demoSamples" :key="s.name" class="btn-ghost" @click="loadSample(s.path)">
              {{ s.name }}
            </button>
          </div>
          <textarea v-model="sourceText" placeholder="粘贴会议纪要，或加载上方示例..." />
          <div class="actions">
            <button class="btn-primary" :disabled="loading || !sourceText.trim()" @click="runWorkflow()">
              {{ loading ? '处理中...' : '开始分析' }}
            </button>
          </div>
        </div>

        <div v-if="progressLog.length" class="card">
          <h3>执行时间线</h3>
          <ul class="progress-list">
            <li v-for="(p, i) in progressLog" :key="i">
              <span class="badge" :class="'kind-' + (p.kind || 'step')">{{ p.kind || 'step' }}</span>
              <span class="badge">{{ p.node }}</span>
              <span v-if="p.route" class="badge route-badge" :class="routeBadgeClass(p.route)">{{ p.route }}</span>
              {{ p.message }}
            </li>
          </ul>
          <ul v-if="toolLog.length" class="progress-list tool-list">
            <li v-for="(t, i) in toolLog" :key="'t'+i">
              <span class="badge kind-tool">tool</span>
              <span class="mono">{{ t.tool }}</span>
              <span v-if="t.query" class="text-muted"> · {{ t.query }}</span>
            </li>
          </ul>
        </div>

        <div v-if="retrieveResults.length" class="card">
          <h3>Playbook Lookup</h3>
          <div v-for="(r, i) in retrieveResults" :key="i" class="retrieve-item">
            <div class="mono">{{ r.source }}</div>
            <p>{{ r.excerpt }}</p>
            <small class="text-muted">{{ r.note }}</small>
          </div>
        </div>
      </section>

      <section class="col-output">
        <!-- HITL: clarify — pinned top -->
        <div v-if="interrupt?.type === 'clarify'" class="card hitl-panel hitl-clarify">
          <div class="hitl-header">
            <span class="hitl-tag tag-clarify">澄清</span>
            <h3>需要补充会议目标</h3>
          </div>
          <p class="hitl-msg">{{ interrupt.message }}</p>
          <input v-model="clarifyInput" placeholder="请输入会议目标..." />
          <div class="actions">
            <button class="btn-primary" :disabled="loading || !clarifyInput.trim()" @click="runWorkflow(clarifyInput)">
              提交澄清
            </button>
          </div>
        </div>

        <!-- HITL: review — pinned top, one-click approve primary -->
        <div v-if="interrupt?.type === 'review'" class="card hitl-panel hitl-review">
          <div class="hitl-header">
            <span class="hitl-tag tag-review">人工审核</span>
            <h3>审核任务清单</h3>
          </div>
          <p class="hitl-msg">{{ interrupt.message }}</p>
          <div class="hitl-primary-actions">
            <button class="btn-success btn-approve" :disabled="loading" @click="resume(true)">
              一键批准并完成
            </button>
            <button class="btn-ghost" :disabled="loading" @click="showReviseForm = !showReviseForm">
              {{ showReviseForm ? '收起修订' : '需要修订…' }}
            </button>
          </div>
          <div v-if="showReviseForm" class="revise-form">
            <label>修订范围</label>
            <select v-model="reviseScope">
              <option value="item">单项 (item)</option>
              <option value="document">全文 (document)</option>
            </select>
            <template v-if="reviseScope === 'item'">
              <label>目标项 ID</label>
              <input v-model="reviseItemId" placeholder="点击下方任务卡片自动填充" />
            </template>
            <label>反馈</label>
            <textarea v-model="reviseFeedback" rows="3" placeholder="修订意见..." />
            <div class="actions">
              <button class="btn-ghost" :disabled="loading" @click="resume(false)">提交修订</button>
            </div>
          </div>
        </div>

        <div v-if="errorMsg" class="card error-card">
          <p>{{ errorMsg }}</p>
        </div>

        <div v-if="snapshot?.meeting_brief" class="card">
          <h3>会议摘要</h3>
          <p><strong>目标：</strong>{{ snapshot.meeting_brief.goal || '—' }}</p>
          <p><strong>干系人：</strong>{{ snapshot.meeting_brief.stakeholders?.join('、') || '—' }}</p>
          <p><strong>日期：</strong>{{ snapshot.meeting_brief.meeting_date || '—' }}</p>
          <p v-if="snapshot.idempotent"><span class="badge badge-warn">幂等返回</span></p>
        </div>

        <div v-if="taskItems.length" class="card">
          <h3>任务清单 <span class="badge">{{ taskItems.length }}</span></h3>
          <div v-for="item in taskItems" :key="item.id" class="task-card" :class="{ selected: reviseItemId === item.id }" @click="selectItemForRevise(item.id)">
            <h4>{{ item.title }}</h4>
            <p class="mono id-line">ID: {{ item.id.slice(0, 8) }}...</p>
            <p><strong>负责人：</strong>{{ item.owner || '待指定' }}</p>
            <p><strong>截止：</strong>{{ item.deadline || '待定' }}</p>
            <p><strong>验收：</strong>{{ item.acceptance_criteria }}</p>
            <p v-if="item.evidence_kind === 'source'"><strong>引用：</strong>{{ item.evidence_quote }}</p>
          </div>
        </div>

        <div v-if="errorIssues.length || advisoryIssues.length" class="card">
          <h3>验证问题</h3>
          <ul>
            <li v-for="(iss, i) in errorIssues" :key="'e'+i" class="badge-error badge">{{ iss.code }}: {{ iss.message }}</li>
            <li v-for="(iss, i) in advisoryIssues" :key="'a'+i" class="badge-warn badge">{{ iss.code }}: {{ iss.message }}</li>
          </ul>
        </div>

        <div v-if="markdownPreview" class="card">
          <h3>Markdown 预览</h3>
          <pre class="md-preview">{{ markdownPreview }}</pre>
          <div class="actions">
            <button class="btn-ghost" @click="download('md')">下载 MD</button>
            <button class="btn-ghost" @click="download('json')">下载 JSON</button>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.app { max-width: 1200px; margin: 0 auto; padding: 1.5rem; }
.header { margin-bottom: 2rem; }
.header h1 { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; }
.subtitle { color: var(--text-muted); margin-top: 0.25rem; }
.main { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; }
.col-output { display: flex; flex-direction: column; gap: 1.25rem; }
.col-output > .card { margin: 0; }
h2, h3 { font-size: 1rem; font-weight: 600; margin-bottom: 0.75rem; }
.sample-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.75rem; align-items: center; }
.actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.progress-list { list-style: none; }
.progress-list li { padding: 0.35rem 0; font-size: 0.875rem; display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; }
.tool-list { margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.5rem; }
.kind-model { background: #1e3a5f; }
.kind-tool { background: #1e4d3a; }
.kind-hitl { background: #5c3d1e; }
.route-badge { text-transform: none; letter-spacing: 0; }
.route-deepseek { background: rgba(61, 139, 253, 0.25); color: #7eb6ff; }
.route-rules { background: rgba(255, 176, 32, 0.2); color: var(--warn); }
.route-hybrid { background: rgba(52, 199, 89, 0.2); color: var(--success); }
.route-pending { background: rgba(143, 163, 184, 0.2); color: var(--text-muted); }
.retrieve-item { padding: 0.5rem 0; border-bottom: 1px solid var(--border); font-size: 0.85rem; }
.retrieve-item:last-child { border: none; }
.text-muted { color: var(--text-muted); }
.task-card { border: 1px solid var(--border); border-radius: 8px; padding: 0.85rem; margin-bottom: 0.65rem; cursor: pointer; transition: border-color 0.15s; }
.task-card:hover, .task-card.selected { border-color: var(--accent); }
.task-card h4 { font-size: 0.9rem; margin-bottom: 0.35rem; }
.id-line { color: var(--text-muted); margin-bottom: 0.35rem; }
.task-card p { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem; }
.revise-form { margin-top: 1rem; padding-top: 0.85rem; border-top: 1px dashed var(--border); }
.revise-form label { display: block; font-size: 0.75rem; color: var(--text-muted); margin: 0.5rem 0 0.25rem; }
.revise-form select { margin-bottom: 0.5rem; }
.error-card { border-color: var(--error); color: var(--error); }
.md-preview { background: rgba(0,0,0,0.4); padding: 1rem; border-radius: 8px; font-family: var(--mono); font-size: 0.75rem; white-space: pre-wrap; max-height: 320px; overflow: auto; }
ul { list-style: none; }
ul li { margin-bottom: 0.35rem; font-size: 0.8rem; }

/* HITL panels */
.hitl-panel {
  order: -1;
  box-shadow: 0 0 0 1px rgba(255, 176, 32, 0.15), 0 8px 24px rgba(0, 0, 0, 0.35);
}
.hitl-clarify {
  border-color: rgba(61, 139, 253, 0.55);
  background: linear-gradient(135deg, rgba(61, 139, 253, 0.12), rgba(18, 24, 32, 0.9));
  border-left: 4px solid var(--accent);
}
.hitl-review {
  border-color: rgba(255, 176, 32, 0.55);
  background: linear-gradient(135deg, rgba(255, 176, 32, 0.12), rgba(18, 24, 32, 0.9));
  border-left: 4px solid var(--warn);
}
.hitl-header {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  margin-bottom: 0.5rem;
}
.hitl-header h3 { margin-bottom: 0; }
.hitl-tag {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  padding: 0.25rem 0.55rem;
  border-radius: 4px;
  text-transform: uppercase;
}
.tag-clarify { background: rgba(61, 139, 253, 0.25); color: #7eb6ff; }
.tag-review { background: rgba(255, 176, 32, 0.25); color: var(--warn); }
.hitl-msg { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 0.75rem; }
.hitl-clarify input { margin: 0.5rem 0; }
.hitl-primary-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
  align-items: center;
  margin-top: 0.5rem;
}
.btn-approve {
  font-size: 1rem;
  padding: 0.75rem 1.35rem;
  box-shadow: 0 0 0 1px rgba(52, 199, 89, 0.4), 0 4px 14px rgba(52, 199, 89, 0.2);
}
</style>
