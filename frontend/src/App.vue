<script setup lang="ts">
/**
 * SpecOps 前端：粘贴纪要 → SSE 看链路 → HITL 澄清/分配/审核 → 导出。
 *
 * 建议 ≠ 写入：点芯片只改本地 assignDrafts，分配须「提交分配」、审核次责须「一键批准」才 resume。
 * confidence 只显示低/中/高，不预填下拉、不按分数自动选。
 * PIPELINE_IDS 必须含 propose_owners，否则 skipped 写不进步进条。
 */
import { ref, computed, reactive, nextTick, watch, onUnmounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Pipeline, { type NodeStatus } from './Pipeline.vue'
import TaskBoard, { type BoardItem } from './TaskBoard.vue'
import { streamSSE, uuid } from './sse'

interface ActionItem extends BoardItem {
  description: string
  deadline: string
  acceptance_criteria: string
  evidence_kind: string
  inference_reason: string
}

interface OwnerCandidate {
  name: string
  role: 'primary' | 'secondary' | string
  confidence: number
  reason: string
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
  markdown_output: string
  idempotent?: boolean
}

interface ProgressEntry {
  node: string
  message: string
  kind?: string
  route?: string | null
  status?: string
}

interface AssignDraft {
  primary_owner: string
  secondary_owners: string[]
}

const PIPELINE_IDS = [
  'intake',
  'clarify',
  'decompose',
  'validate_repair',
  'propose_owners',
  'assign_owners',
  'review',
  'finalize',
]

const sourceText = ref('')
const progressLog = ref<ProgressEntry[]>([])
const toolLog = ref<Array<Record<string, unknown>>>([])
const snapshot = ref<Snapshot | null>(null)
const nodeStatus = reactive<Record<string, NodeStatus>>({})
const assignDrafts = reactive<Record<string, AssignDraft>>({})

const MODEL_NODES = new Set(['intake', 'decompose', 'propose_owners', 'clarify'])
const TOOL_NODES = new Set(['validate_repair', 'finalize'])
const HITL_NODES = new Set(['review', 'assign_owners', 'clarify'])

function resetPipeline() {
  // 每次新开跑把八点都打回 pending，避免上次 skipped 残留
  for (const id of PIPELINE_IDS) nodeStatus[id] = 'pending'
  for (const key of Object.keys(assignDrafts)) delete assignDrafts[key]
}

function stepKind(node: string): string {
  if (MODEL_NODES.has(node)) return 'model'
  if (TOOL_NODES.has(node) || node.startsWith('validate')) return 'tool'
  if (HITL_NODES.has(node)) return 'hitl'
  return 'step'
}

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

function pushProgress(d: { node: string; message: string; status?: string }) {
  // 只认 PIPELINE_IDS 里的节点，避免未知 node 把步进条弄乱
  progressLog.value.push({
    ...d,
    kind: stepKind(d.node),
    route: parseRoute(d.message),
  })
  if (d.status && PIPELINE_IDS.includes(d.node)) {
    nodeStatus[d.node] = d.status as NodeStatus
  }
}

const interrupt = ref<Record<string, unknown> | null>(null)
const clarifyInput = ref('')
const reviseScope = ref<'item' | 'document' | 'drop'>('item')
const reviseItemId = ref('')
const reviseFeedback = ref('')
const showReviseForm = ref(false)
const loading = ref(false)
const errorMsg = ref('')
const notice = ref('')
const threadId = ref<string | null>(null)
const locateHint = ref('')
const expand = ref(false)
const sourceMode = ref<'edit' | 'read'>('edit')
const quoteRange = ref<{ start: number; end: number } | null>(null)
const sourceRead = ref<HTMLElement | null>(null)
const progressListEl = ref<HTMLElement | null>(null)
let runSeq = 0
let abortCtrl: AbortController | null = null
const SOFT_LIMIT = 8000
const HARD_LIMIT = 100000

const sampleButtons = [
  { name: '示例1', path: '/samples/demo_sprint_planning.txt' },
  { name: '示例2', path: '/samples/demo_incident_review.txt' },
  { name: '示例3', path: '/samples/demo_feature_review.txt' },
]

const taskItems = computed(() => snapshot.value?.task_draft?.items ?? [])
const stakeholders = computed(() => snapshot.value?.meeting_brief?.stakeholders ?? [])
const errorIssues = computed(() =>
  snapshot.value?.validation?.issues?.filter(i => i.severity === 'error') ?? []
)
const advisoryIssues = computed(() =>
  snapshot.value?.validation?.issues?.filter(i => i.severity === 'advisory') ?? []
)
const markdownPreview = computed(() => snapshot.value?.markdown_output ?? '')
const mdView = ref<'preview' | 'source'>('preview')
const markdownHtml = computed(() => {
  if (!markdownPreview.value) return ''
  const html = marked.parse(markdownPreview.value, { async: false }) as string
  return DOMPurify.sanitize(html)
})
const assignItems = computed(() => (interrupt.value?.items as Array<Record<string, unknown>>) ?? [])
const reviewItems = computed(() => (interrupt.value?.items as Array<Record<string, unknown>>) ?? [])

function effectivePrimary(item: { id?: unknown; primary_owner?: unknown }) {
  const id = String(item.id || '')
  return assignDrafts[id]?.primary_owner || String(item.primary_owner || '')
}

const reviewPendingPrimary = computed(() =>
  reviewItems.value.filter(item => !effectivePrimary(item).trim()).length,
)
const assignStakeholders = computed(
  () => (interrupt.value?.stakeholders as string[] | undefined) ?? stakeholders.value,
)

function itemSuggestions(item: Record<string, unknown>): OwnerCandidate[] {
  /** interrupt payload 上的建议；resume 前主责仍应为空。 */
  const raw = item.owner_suggestions
  if (!Array.isArray(raw)) return []
  return raw as OwnerCandidate[]
}

function confidenceBand(value: number): '低' | '中' | '高' {
  // 展示分档，不当门槛，也不用来排序
  if (value < 0.4) return '低'
  if (value < 0.7) return '中'
  return '高'
}

function firstSuggestion(item: Record<string, unknown>): OwnerCandidate | null {
  // 「采纳全部」用列表顺序第一条，不按 confidence 数值
  const list = itemSuggestions(item)
  return list.find(s => s.role !== 'secondary') || list[0] || null
}

const canAdoptAll = computed(() => {
  // 任一将写入的名字不在干系人名单 → 禁用，避免一键 INVALID_PRIMARY
  const names = new Set(assignStakeholders.value)
  let any = false
  for (const item of assignItems.value) {
    const first = firstSuggestion(item)
    if (!first?.name) continue
    any = true
    if (!names.has(first.name)) return false
  }
  return any
})

function applySuggestion(itemId: string, cand: OwnerCandidate) {
  // 只改本地草稿，不发 resume
  const draft = assignDrafts[itemId]
  if (!draft || !cand.name) return
  if (cand.role === 'secondary') {
    if (draft.primary_owner === cand.name) return
    if (!draft.secondary_owners.includes(cand.name)) {
      draft.secondary_owners = [...draft.secondary_owners, cand.name]
    }
    return
  }
  draft.primary_owner = cand.name
  draft.secondary_owners = draft.secondary_owners.filter(n => n !== cand.name)
}

function adoptAllSuggestions() {
  if (!canAdoptAll.value) return
  const names = new Set(assignStakeholders.value)
  for (const item of assignItems.value) {
    const id = String(item.id || '')
    if (!id) continue
    const first = firstSuggestion(item)
    if (first) applySuggestion(id, first)
    for (const cand of itemSuggestions(item)) {
      if (cand.role === 'secondary' && names.has(cand.name)) {
        applySuggestion(id, cand)
      }
    }
  }
}

function seedAssignDrafts(payload: Record<string, unknown>) {
  // 用 interrupt 里已有的主责初始化；建议不预填 select，避免「建议被当成已写入」
  const items = (payload.items as Array<Record<string, unknown>>) || []
  for (const item of items) {
    const id = String(item.id || '')
    if (!id) continue
    assignDrafts[id] = {
      primary_owner: String(item.primary_owner || ''),
      secondary_owners: Array.isArray(item.secondary_owners)
        ? (item.secondary_owners as string[])
        : [],
    }
  }
}

async function loadSample(path: string) {
  try {
    const resp = await fetch(path)
    sourceText.value = await resp.text()
  } catch {
    errorMsg.value = '无法加载示例文件，请手动粘贴'
  }
}

function makeHandlers(mine: () => boolean) {
  // 每个入口自己的闭包。旧流迟到回调看到代次不对就丢掉，避免关掉新一次的 loading。
  return {
    onMetadata: (d: Record<string, unknown>) => {
      if (!mine()) return
      if (d.thread_id) threadId.value = d.thread_id as string
    },
    onProgress: (d: { node: string; message: string; status?: string }) => {
      if (!mine()) return
      pushProgress(d)
    },
    onToolResult: (d: Record<string, unknown>) => {
      if (!mine()) return
      toolLog.value.push(d)
    },
    onInterrupt: (d: Record<string, unknown>) => {
      if (!mine()) return
      interrupt.value = d
      const t = String(d.type || '')
      if (PIPELINE_IDS.includes(t)) nodeStatus[t] = 'waiting'
      if (t === 'assign_owners' || t === 'review') seedAssignDrafts(d)
    },
    onResult: (d: Record<string, unknown>) => {
      if (!mine()) return
      snapshot.value = d as unknown as Snapshot
    },
    onError: (d: { message: string }) => {
      if (!mine()) return
      errorMsg.value = d.message
    },
    onDone: () => {
      if (!mine()) return
      loading.value = false
    },
  }
}

function startControlledRun() {
  const seq = ++runSeq
  abortCtrl?.abort()
  const ctrl = new AbortController()
  abortCtrl = ctrl
  return { mine: () => seq === runSeq, signal: ctrl.signal }
}

function cancelRun() {
  ++runSeq
  abortCtrl?.abort()
  abortCtrl = null
  loading.value = false
  notice.value = '已取消本次分析'
}

async function runWorkflow(clarify?: string) {
  // 澄清走 /chat/stream 而不是 /resume，因为 clarify interrupt 绑在同一 graph_ns
  const { mine, signal } = startControlledRun()
  loading.value = true
  errorMsg.value = ''
  notice.value = ''
  progressLog.value = []
  toolLog.value = []
  interrupt.value = null
  showReviseForm.value = false
  if (!clarify) {
    resetPipeline()
    nodeStatus.intake = 'running'
  }

  try {
    await streamSSE('/api/v1/chat/stream', {
      source_text: sourceText.value,
      thread_id: threadId.value,
      request_id: uuid(),
      clarify_response: clarify || null,
    }, makeHandlers(mine), signal)
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    if (mine()) loading.value = false
  }
}

async function resume(approve = false) {
  // 分配必须带 assignments 且 approve=false，避免误走「一键批准」分支
  if (!snapshot.value?.run_id) return
  const { mine, signal } = startControlledRun()
  loading.value = true
  errorMsg.value = ''
  notice.value = ''
  const type = String(interrupt.value?.type || 'review')
  interrupt.value = null
  showReviseForm.value = false

  const body: Record<string, unknown> = {
    request_id: uuid(),
    interrupt_type: type,
    approve,
    revision_scope: reviseScope.value,
    target_item_id: reviseScope.value === 'document' ? null : (reviseItemId.value || null),
    feedback: reviseFeedback.value,
  }
  if (reviseScope.value === 'drop' && reviseItemId.value) {
    body.drop_item_ids = [reviseItemId.value]
  }
  if (type === 'assign_owners' || (type === 'review' && approve)) {
    body.assignments = Object.entries(assignDrafts).map(([item_id, draft]) => ({
      item_id,
      primary_owner: draft.primary_owner,
      secondary_owners: draft.secondary_owners,
    }))
    if (type === 'assign_owners') body.approve = false
  }

  try {
    await streamSSE(`/api/v1/runs/${snapshot.value.run_id}/resume`, body, makeHandlers(mine), signal)
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    if (mine()) loading.value = false
  }
}

async function resumeDrop(itemId: string) {
  // 审核面板「不是任务」：显式 drop，不依赖自然语言解析
  if (!snapshot.value?.run_id) return
  const { mine, signal } = startControlledRun()
  loading.value = true
  errorMsg.value = ''
  notice.value = ''
  interrupt.value = null
  showReviseForm.value = false
  try {
    await streamSSE(
      `/api/v1/runs/${snapshot.value.run_id}/resume`,
      {
        request_id: uuid(),
        interrupt_type: 'review',
        approve: false,
        revision_scope: 'drop',
        drop_item_ids: [itemId],
        feedback: '',
      },
      makeHandlers(mine),
      signal,
    )
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    if (mine()) loading.value = false
  }
}

function toggleSecondary(itemId: string, name: string) {
  // 主责那人不能再当次责，否则后端 PRIMARY_IN_SECONDARY
  const draft = assignDrafts[itemId]
  if (!draft) return
  const set = new Set(draft.secondary_owners)
  if (set.has(name)) set.delete(name)
  else set.add(name)
  draft.secondary_owners = [...set].filter(n => n !== draft.primary_owner)
}

function download(format: 'md' | 'json') {
  if (!threadId.value) return
  window.open(`/api/v1/threads/${threadId.value}/export?format=${format}`, '_blank')
}

function asBoardItem(raw: Record<string, unknown> | BoardItem): BoardItem {
  const item = raw as BoardItem & { description?: string }
  return {
    id: String(item.id || ''),
    title: String(item.title || ''),
    description: String(item.description || ''),
    primary_owner: String(item.primary_owner || ''),
    secondary_owners: Array.isArray(item.secondary_owners) ? item.secondary_owners : [],
    evidence_quote: String(item.evidence_quote || ''),
    unresolved_ref: String(item.unresolved_ref || ''),
  }
}

function quoteCandidates(item: BoardItem): string[] {
  // 定位原文：先 evidence_quote，再 description / title；推断任务往往没有连续引用
  const extra = (item as ActionItem).description
  const out: string[] = []
  for (const raw of [item.evidence_quote, extra, item.title]) {
    const text = (raw || '').trim()
    if (text && !out.includes(text)) out.push(text)
  }
  return out
}

function findQuoteRange(haystack: string, item: BoardItem): { start: number; end: number } | null {
  for (const quote of quoteCandidates(item)) {
    const start = haystack.indexOf(quote)
    if (start >= 0) return { start, end: start + quote.length }
  }
  return null
}

const quoteParts = computed(() => {
  const range = quoteRange.value
  if (!range) return null
  const text = sourceText.value
  return {
    before: text.slice(0, range.start),
    hit: text.slice(range.start, range.end),
    after: text.slice(range.end),
  }
})

async function locateInSource(item: BoardItem) {
  // 切到只读层用 <mark> 定位，不 focus textarea，所以不用 suppressLocate。
  // 不要改回叠在 textarea 上的高亮层：两套视图不对齐就不存在像素错位。
  locateHint.value = ''
  const range = findQuoteRange(sourceText.value, item)
  if (!range) {
    quoteRange.value = null
    locateHint.value =
      (item as ActionItem).evidence_kind === 'inference'
        ? '该项是推断任务，原文没有可定位的连续引用'
        : '未在当前原文中找到对应引用'
    return
  }
  quoteRange.value = range
  sourceMode.value = 'read'
  await nextTick()
  await nextTick()
  sourceRead.value?.querySelector('mark')?.scrollIntoView({ block: 'center' })
}

function editSource() {
  sourceMode.value = 'edit'
}

function selectItemForRevise(item: BoardItem) {
  reviseItemId.value = item.id
  reviseScope.value = 'item'
  showReviseForm.value = true
  void locateInSource(item)
}

function isHitlInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  return Boolean(
    target.closest('select, option, button, input, textarea, label, .assign-controls'),
  )
}

function locateHitlItem(raw: Record<string, unknown>, ev?: Event) {
  // 只有点标题区才定位；select/button 点击会被吞掉，避免抢走下拉焦点
  if (isHitlInteractiveTarget(ev?.target ?? null)) return
  const item = asBoardItem(raw)
  reviseItemId.value = item.id
  void locateInSource(item)
}

function onEsc(e: KeyboardEvent) {
  if (e.key === 'Escape' && expand.value) expand.value = false
}

watch(expand, (on) => {
  document.body.style.overflow = on ? 'hidden' : ''
  if (on) window.addEventListener('keydown', onEsc)
  else window.removeEventListener('keydown', onEsc)
})

watch(() => progressLog.value.length + toolLog.value.length, async () => {
  const el = progressListEl.value
  if (!el || el.clientHeight === 0) return
  const near = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  if (!near) return
  await nextTick()
  const box = progressListEl.value
  if (!box || box.clientHeight === 0) return
  box.scrollTop = box.scrollHeight
})

onUnmounted(() => {
  window.removeEventListener('keydown', onEsc)
  document.body.style.overflow = ''
  abortCtrl?.abort()
})

resetPipeline()
</script>

<template>
  <div class="app">
    <header class="header">
      <div class="header-inner">
        <h1>会议纪要助手</h1>
      </div>
    </header>

    <div class="card pipeline-card">
      <h3>执行链路</h3>
      <Pipeline :statuses="nodeStatus" />
    </div>

    <main class="main grid-2">
      <section class="col-input">
        <div class="card">
          <h2>会议对话</h2>
          <div class="sample-row">
            <button v-for="s in sampleButtons" :key="s.name" class="btn-ghost" @click="loadSample(s.path)">
              {{ s.name }}
            </button>
          </div>
          <!-- 只读层与 textarea 切换，不要叠成高亮层：不对齐就没有像素错位 -->
          <!-- Teleport：.card 的 backdrop-filter 会把 fixed 困在卡片里 -->
          <Teleport to="body" :disabled="!expand">
          <div class="source-wrap" :class="{ expanded: expand }">
            <textarea
              v-show="sourceMode === 'edit'"
              v-model="sourceText"
              placeholder="粘贴会议对话，或加载上方示例..."
            />
            <div v-show="sourceMode === 'read'" ref="sourceRead" class="source-read">
              <template v-if="quoteParts">{{ quoteParts.before }}<mark>{{ quoteParts.hit }}</mark>{{ quoteParts.after }}</template>
              <template v-else>{{ sourceText }}</template>
            </div>
            <div class="source-toolbar">
              <button v-if="sourceMode === 'read'" class="btn-ghost" type="button" @click="editSource">编辑</button>
              <button class="btn-ghost" type="button" @click="expand = !expand">
                {{ expand ? '收起' : '放大编辑' }}
              </button>
            </div>
          </div>
          </Teleport>
          <p class="text-muted char-count">
            {{ sourceText.length }} 字
            <span v-if="sourceText.length > SOFT_LIMIT && sourceText.length <= HARD_LIMIT"> · 将分块抽取，耗时更长</span>
            <span v-else-if="sourceText.length > HARD_LIMIT"> · 超出上限，无法提交</span>
          </p>
          <p v-if="locateHint" class="locate-hint">{{ locateHint }}</p>
          <div class="actions">
            <button
              class="btn-primary"
              :disabled="loading || !sourceText.trim() || sourceText.length > HARD_LIMIT"
              @click="runWorkflow()"
            >
              {{ loading ? '处理中...' : '开始分析' }}
            </button>
            <button v-if="loading" class="btn-ghost" type="button" @click="cancelRun">取消</button>
          </div>
          <p v-if="loading || notice" class="text-muted cancel-note">
            {{ notice || '取消只停止本页接收，后端仍在跑，立刻再开始可能提示线程忙碌。' }}
          </p>
        </div>

        <details v-if="progressLog.length" class="card timeline" open>
          <summary>执行时间线</summary>
          <div ref="progressListEl" class="progress-list">
            <ul>
              <li v-for="(p, i) in progressLog" :key="i">
                <span class="badge" :class="'kind-' + (p.kind || 'step')">{{ p.kind || 'step' }}</span>
                <span class="badge">{{ p.node }}</span>
                <span v-if="p.status" class="badge">{{ p.status }}</span>
                <span v-if="p.route" class="badge route-badge" :class="routeBadgeClass(p.route)">{{ p.route }}</span>
                {{ p.message }}
              </li>
            </ul>
            <ul v-if="toolLog.length" class="tool-list">
              <li v-for="(t, i) in toolLog" :key="'t'+i">
                <span class="badge kind-tool">tool</span>
                <span class="mono">{{ t.tool }}</span>
              </li>
            </ul>
          </div>
        </details>
      </section>

      <section class="col-output">
        <div v-if="interrupt?.type === 'clarify'" class="card hitl-panel hitl-clarify span-2">
          <!-- 澄清走 runWorkflow(clarifyInput)，不是 /resume -->
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

        <div v-if="interrupt?.type === 'assign_owners'" class="card hitl-panel hitl-assign span-2">
          <!-- 建议芯片只改 assignDrafts；提交后才 resume。整行不要 @click 定位 -->
          <div class="hitl-header">
            <span class="hitl-tag tag-assign">分配负责人</span>
            <h3>无法从原文确认主责的任务</h3>
          </div>
          <p class="hitl-msg">{{ interrupt.message }}</p>
          <div
            v-for="item in assignItems"
            :key="String(item.id)"
            class="assign-row"
            :class="{ selected: reviseItemId === String(item.id) }"
          >
            <div class="assign-locate" @click="locateHitlItem(item, $event)">
              <h4>{{ item.title }}</h4>
              <p v-if="item.unresolved_ref" class="text-muted">原文未点名：{{ item.unresolved_ref }}（点击定位原文）</p>
              <p class="text-muted">{{ item.description }}</p>
              <p v-if="item.evidence_quote" class="text-muted">引用：{{ item.evidence_quote }}</p>
            </div>
            <div v-if="assignDrafts[String(item.id)]" class="assign-controls" @click.stop @mousedown.stop>
            <label>主责</label>
            <select
              v-model="assignDrafts[String(item.id)].primary_owner"
              @mousedown.stop
              @click.stop
            >
              <option value="">请选择</option>
              <option v-for="name in assignStakeholders" :key="name" :value="name">
                {{ name }}
              </option>
            </select>
            <label>次责（可多选）</label>
            <div class="chip-row">
              <button
                v-for="name in assignStakeholders"
                :key="name"
                type="button"
                class="chip"
                :class="{ on: assignDrafts[String(item.id)]?.secondary_owners.includes(name) }"
                :disabled="assignDrafts[String(item.id)]?.primary_owner === name"
                @click.stop="toggleSecondary(String(item.id), name)"
              >
                {{ name }}
              </button>
            </div>
            <div v-if="itemSuggestions(item).length" class="suggest-block" @click.stop>
              <label>模型建议（仅供参考，点选后仍须提交）</label>
              <div class="chip-row">
                <button
                  v-for="(cand, idx) in itemSuggestions(item)"
                  :key="String(item.id) + cand.name + idx"
                  type="button"
                  class="suggest-chip"
                  :class="{
                    on: cand.role === 'secondary'
                      ? assignDrafts[String(item.id)]?.secondary_owners.includes(cand.name)
                      : assignDrafts[String(item.id)]?.primary_owner === cand.name,
                  }"
                  @click.stop="applySuggestion(String(item.id), cand)"
                >
                  <span>{{ cand.name }}</span>
                  <span class="suggest-band">{{ cand.role === 'secondary' ? '次责' : '主责' }} · {{ confidenceBand(Number(cand.confidence) || 0) }}</span>
                </button>
              </div>
              <p v-if="itemSuggestions(item)[0]?.reason" class="text-muted suggest-reason">
                {{ itemSuggestions(item)[0].reason }}
              </p>
            </div>
            </div>
          </div>
          <div class="hitl-primary-actions">
            <button class="btn-ghost" :disabled="loading || !canAdoptAll" @click="adoptAllSuggestions">
              采纳全部
            </button>
            <button class="btn-primary" :disabled="loading" @click="resume(false)">提交分配</button>
          </div>
        </div>

        <div v-if="interrupt?.type === 'review'" class="card hitl-panel hitl-review span-2">
          <!-- 默认一键批准；删除用全局序号或 UUID，不要用看板上的「某人第 N 条」 -->
          <div class="hitl-header">
            <span class="hitl-tag tag-review">人工审核</span>
            <h3>审核任务清单</h3>
          </div>
          <p class="hitl-msg">{{ interrupt.message }}</p>
          <p class="text-muted locate-help">点击下方任务可在左侧原文中定位。序号为全局顺序，删除请用「第 N 条」。共 {{ reviewItems.length }} 条 · 主责待确认 {{ reviewPendingPrimary }} 条。</p>
          <div
            v-for="item in reviewItems"
            :key="String(item.id)"
            class="assign-row"
            :class="{ selected: reviseItemId === String(item.id) }"
            @click="locateHitlItem(item); reviseItemId = String(item.id)"
          >
            <div class="review-row-head">
              <h4><span class="review-index">{{ item.index }}.</span> {{ item.title }}</h4>
              <button
                class="btn-ghost btn-drop"
                :disabled="loading"
                @click.stop="resumeDrop(String(item.id))"
              >删除</button>
            </div>
            <p class="text-muted">主责：{{ effectivePrimary(item) || '—' }}</p>
            <p class="text-muted">
              次责：{{
                (assignDrafts[String(item.id)]?.secondary_owners.length
                  ? assignDrafts[String(item.id)].secondary_owners
                  : (item.secondary_owners as string[] | undefined) || []
                ).join('、') || '—'
              }}
            </p>
            <p v-if="item.evidence_quote" class="text-muted">引用：{{ item.evidence_quote }}</p>
            <div
              v-if="itemSuggestions(item).length && assignDrafts[String(item.id)]"
              class="suggest-block"
              @click.stop
            >
              <label>模型建议（仅供参考，点选后仍须批准）</label>
              <div class="chip-row">
                <button
                  v-for="(cand, idx) in itemSuggestions(item)"
                  :key="String(item.id) + cand.name + idx"
                  type="button"
                  class="suggest-chip"
                  :class="{
                    on: cand.role === 'secondary'
                      ? assignDrafts[String(item.id)]?.secondary_owners.includes(cand.name)
                      : assignDrafts[String(item.id)]?.primary_owner === cand.name,
                  }"
                  @click.stop="applySuggestion(String(item.id), cand)"
                >
                  <span>{{ cand.name }}</span>
                  <span class="suggest-band">{{ cand.role === 'secondary' ? '次责' : '主责' }} · {{ confidenceBand(Number(cand.confidence) || 0) }}</span>
                </button>
              </div>
            </div>
          </div>
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
              <option value="drop">删除 (drop)</option>
            </select>
            <template v-if="reviseScope !== 'document'">
              <label>目标项 ID</label>
              <input v-model="reviseItemId" placeholder="点击任务卡片自动填充；删除也可只写「第 2 条删掉」" />
            </template>
            <label>反馈</label>
            <textarea v-model="reviseFeedback" rows="3" placeholder="例如：第 2 条不是任务，删掉" />
            <div class="actions">
              <button class="btn-ghost" :disabled="loading" @click="resume(false)">提交修订</button>
            </div>
          </div>
        </div>

        <div v-if="errorMsg" class="card error-card span-2">
          <p>{{ errorMsg }}</p>
        </div>

        <div v-if="snapshot?.meeting_brief" class="card">
          <h3>会议摘要</h3>
          <p><strong>目标：</strong>{{ snapshot.meeting_brief.goal || '—' }}</p>
          <p><strong>干系人：</strong>{{ snapshot.meeting_brief.stakeholders?.join('、') || '—' }}</p>
          <p><strong>日期：</strong>{{ snapshot.meeting_brief.meeting_date || '—' }}</p>
          <p v-if="snapshot.idempotent"><span class="badge badge-warn">幂等返回</span></p>
        </div>

        <div v-if="taskItems.length" class="card span-2">
          <h3>按人任务 <span class="badge">{{ taskItems.length }}</span></h3>
          <TaskBoard :items="taskItems" :selected-id="reviseItemId" @select="selectItemForRevise" />
        </div>

        <div v-if="errorIssues.length || advisoryIssues.length" class="card">
          <h3>验证问题</h3>
          <ul>
            <li v-for="(iss, i) in errorIssues" :key="'e'+i" class="badge-error badge">{{ iss.code }}: {{ iss.message }}</li>
            <li v-for="(iss, i) in advisoryIssues" :key="'a'+i" class="badge-warn badge">{{ iss.code }}: {{ iss.message }}</li>
          </ul>
        </div>

        <div v-if="markdownPreview" class="card span-2">
          <h3>Markdown 预览</h3>
          <div class="actions md-toggle">
            <button class="btn-ghost" :class="{ on: mdView === 'preview' }" @click="mdView = 'preview'">预览</button>
            <button class="btn-ghost" :class="{ on: mdView === 'source' }" @click="mdView = 'source'">源码</button>
          </div>
          <div v-if="mdView === 'preview'" class="md-preview" v-html="markdownHtml" />
          <pre v-else class="md-source">{{ markdownPreview }}</pre>
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
.app { max-width: min(100% - 2.5rem, 1600px); margin: 0 auto; padding: 1.5rem 1.25rem; }
.header { margin-bottom: 1rem; }
.header h1 { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; }
.pipeline-card { margin-bottom: 1.25rem; }
.main { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); gap: 1.5rem; align-items: start; }
.col-input { display: flex; flex-direction: column; gap: 1.25rem; }
.col-output { display: flex; flex-direction: column; gap: 1.25rem; }
.col-output > .card { margin: 0; }
@media (min-width: 1500px) {
  .col-output { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; align-content: start; }
  .col-output > .span-2 { grid-column: 1 / -1; }
}
h2, h3 { font-size: 1rem; font-weight: 600; margin-bottom: 0.75rem; }
.sample-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.75rem; align-items: center; }
.actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.progress-list { max-height: 260px; overflow-y: auto; }
.progress-list ul { list-style: none; }
.progress-list li { padding: 0.28rem 0; font-size: 0.8rem; display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; }
.timeline summary { font-size: 1rem; font-weight: 600; cursor: pointer; margin-bottom: 0.75rem; }
.source-wrap { position: relative; }
.source-wrap textarea { padding-bottom: 2.2rem; }
.source-read {
  min-height: 140px;
  max-height: 62vh;
  overflow-y: auto;
  white-space: pre-wrap;
  font-family: var(--font);
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  padding: 0.65rem 0.85rem 2.2rem;
  width: 100%;
  font-size: 0.9rem;
  line-height: 1.5;
}
.source-read mark { background: rgba(255, 176, 32, 0.35); color: inherit; }
.source-toolbar {
  position: absolute;
  right: 0.6rem;
  bottom: 0.6rem;
  display: flex;
  gap: 0.35rem;
}
.source-toolbar .btn-ghost { font-size: 0.75rem; padding: 0.25rem 0.6rem; }
.expanded {
  position: fixed;
  inset: 5vh 6vw;
  z-index: 60;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #121820;
  padding: 1rem;
  border-radius: 12px;
  border: 1px solid var(--border);
}
.expanded textarea,
.expanded .source-read {
  flex: 1;
  field-sizing: fixed;
  resize: none;
  max-height: none;
  min-height: 0;
  height: auto;
}
.expanded .source-toolbar { position: static; margin-top: 0.6rem; align-self: flex-end; }
.cancel-note { font-size: 0.75rem; margin-top: 0.45rem; }
.tool-list { margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.5rem; }
.kind-model { background: #1e3a5f; }
.kind-tool { background: #1e4d3a; }
.kind-hitl { background: #5c3d1e; }
.route-badge { text-transform: none; letter-spacing: 0; }
.route-deepseek { background: rgba(61, 139, 253, 0.25); color: #7eb6ff; }
.route-rules { background: rgba(255, 176, 32, 0.2); color: var(--warn); }
.route-hybrid { background: rgba(52, 199, 89, 0.2); color: var(--success); }
.route-pending { background: rgba(143, 163, 184, 0.2); color: var(--text-muted); }
.text-muted { color: var(--text-muted); }
.task-card { border: 1px solid var(--border); border-radius: 8px; padding: 0.85rem; margin-bottom: 0.65rem; cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s; }
.task-card:hover, .task-card.selected { border-color: var(--accent); }
.task-card.selected { box-shadow: 0 0 0 1px rgba(61, 139, 253, 0.35); }
.locate-help { font-size: 0.75rem; margin: -0.35rem 0 0.65rem; }
.locate-hint { color: var(--warn); font-size: 0.8rem; margin-top: 0.45rem; }
.task-card h4 { font-size: 0.9rem; margin-bottom: 0.35rem; }
.id-line { color: var(--text-muted); margin-bottom: 0.35rem; }
.task-card p { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem; }
.revise-form { margin-top: 1rem; padding-top: 0.85rem; border-top: 1px dashed var(--border); }
.revise-form label, .assign-row label { display: block; font-size: 0.75rem; color: var(--text-muted); margin: 0.5rem 0 0.25rem; }
.revise-form select { margin-bottom: 0.5rem; }
.error-card { border-color: var(--error); color: var(--error); }
.md-toggle { margin: 0 0 0.75rem; }
.md-toggle .on { border-color: var(--accent); color: var(--accent); }
.md-preview {
  background: rgba(0,0,0,0.4);
  padding: 1rem 1.15rem;
  border-radius: 8px;
  max-height: 560px;
  overflow: auto;
  font-size: 0.85rem;
  line-height: 1.55;
  color: var(--text);
}
.md-preview :deep(h1) { font-size: 1.15rem; margin: 0 0 0.75rem; }
.md-preview :deep(h2) { font-size: 0.95rem; margin: 1rem 0 0.45rem; }
.md-preview :deep(ol) { padding-left: 1.2rem; margin: 0.25rem 0 0.5rem; }
.md-preview :deep(ul) { padding-left: 1.2rem; margin: 0.25rem 0 0.5rem; }
.md-preview :deep(strong) { font-weight: 650; }
.md-preview :deep(code) { font-family: var(--mono); font-size: 0.8rem; }
.md-preview :deep(details) { margin-top: 0.75rem; color: var(--text-muted); }
.md-preview :deep(summary) { cursor: pointer; color: var(--text); }
.md-source {
  background: rgba(0,0,0,0.4);
  padding: 1rem;
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 0.75rem;
  white-space: pre-wrap;
  max-height: 560px;
  overflow: auto;
}
ul { list-style: none; }
ul li { margin-bottom: 0.35rem; font-size: 0.8rem; }
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
.hitl-assign {
  border-color: rgba(52, 199, 89, 0.45);
  background: linear-gradient(135deg, rgba(52, 199, 89, 0.1), rgba(18, 24, 32, 0.9));
  border-left: 4px solid var(--success);
}
.hitl-header { display: flex; align-items: center; gap: 0.65rem; margin-bottom: 0.5rem; }
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
.tag-assign { background: rgba(52, 199, 89, 0.22); color: var(--success); }
.hitl-msg { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 0.75rem; }
.hitl-clarify input { margin: 0.5rem 0; }
.hitl-primary-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
  align-items: center;
  position: sticky;
  bottom: 0;
  z-index: 2;
  padding: 0.85rem 0 0.4rem;
  margin-top: 0.75rem;
  background: linear-gradient(180deg, rgba(18, 24, 32, 0) 0%, rgba(18, 24, 32, 0.94) 35%);
  backdrop-filter: blur(6px);
  border-top: 1px solid var(--border);
}
.btn-approve {
  font-size: 1rem;
  padding: 0.75rem 1.35rem;
  box-shadow: 0 0 0 1px rgba(52, 199, 89, 0.4), 0 4px 14px rgba(52, 199, 89, 0.2);
}
.assign-row { border-top: 1px dashed var(--border); padding: 0.75rem 0; }
.assign-row.selected { border-color: var(--accent); }
.assign-row h4 { font-size: 0.9rem; margin-bottom: 0.35rem; }
.review-row-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem; }
.review-index { color: var(--warn); font-variant-numeric: tabular-nums; margin-right: 0.15rem; }
.btn-drop { padding: 0.2rem 0.55rem; font-size: 0.75rem; flex-shrink: 0; }
.char-count { font-size: 0.75rem; margin: 0.35rem 0 0; }
.assign-locate { cursor: pointer; }
.assign-controls { cursor: default; }
.chip-row { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.chip {
  background: transparent;
  color: var(--text-muted);
  border: 1px solid var(--border);
  padding: 0.25rem 0.55rem;
  font-size: 0.75rem;
}
.chip.on { border-color: var(--success); color: var(--success); background: rgba(52, 199, 89, 0.12); }
.suggest-block { margin-top: 0.55rem; }
.suggest-chip {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.1rem;
  background: transparent;
  color: var(--text);
  border: 1px solid var(--border);
  padding: 0.3rem 0.6rem;
  font-size: 0.8rem;
  text-align: left;
}
.suggest-chip.on { border-color: var(--accent); color: var(--accent); background: rgba(61, 139, 253, 0.12); }
.suggest-band { font-size: 0.75rem; color: var(--text-muted); }
.suggest-reason { font-size: 0.75rem; margin-top: 0.35rem; }
</style>
