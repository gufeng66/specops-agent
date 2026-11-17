<script setup lang="ts">
/**
 * SpecOps 前端：粘贴纪要 → SSE 看链路 → HITL 澄清/分配/审核 → 导出。
 *
 * 建议 ≠ 写入：点芯片只改本地 assignDrafts，分配须「提交分配」、审核次责须「一键批准」才 resume。
 * confidence 只显示低/中/高，不预填下拉、不按分数自动选。
 * PIPELINE_IDS 必须含 propose_owners，否则 skipped 写不进步进条。
 */
import { ref, computed, reactive, nextTick, watch, onUnmounted, onMounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import Pipeline, { type NodeStatus } from './Pipeline.vue'
import ProgressLog from './ProgressLog.vue'
import SourcePane from './SourcePane.vue'
import TaskBoard, { type BoardItem } from './TaskBoard.vue'
import { streamSSE, uuid } from './sse'
import { acceptResponse, acceptSnapshot, confirmHeadline, confirmLabels, confirmOwner, fieldChanges, isSettledStatus, issueField, moveTabIndex, needsSnapshotRefresh, nextInterrupt, overlayReviewItem, ownerOriginals, rememberedThread, rememberThread, resolveDraft, reviewOrder, shouldFollowPlanning, viewPhase, type IssueSlot } from './session'
import { findQuoteRange, isHitlInteractiveTarget } from './sourceLocation'

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

interface ProblemIssue {
  code: string
  message: string
  severity: string
  item_id?: string | null
  field?: string | null
}

interface Extraction {
  chunks_total?: number
  chunks_ok?: number
  chunks_empty?: number
  chunks_failed?: number
  partial?: boolean
  degraded?: boolean
  detail?: string
}

interface Snapshot {
  thread_id: string
  run_id: string
  status: string
  /** 逻辑版本：只接受不倒退的版本，旧响应不能覆盖新状态。 */
  revision?: number
  schema_version?: number
  checkpoint_id?: string
  source_text: string
  meeting_brief: { goal: string; stakeholders: string[]; meeting_date: string }
  task_draft: { items: ActionItem[] }
  validation: { issues: ProblemIssue[] }
  extraction?: Extraction | null
  review?: {
    override_reason?: string | null
    override_codes?: string[]
    override_at?: string | null
    operator_claim?: string | null
  } | null
  interrupt_id?: string | null
  interrupt_payload?: Record<string, unknown> | null
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
const reviseTargetId = ref('')
const reviseFeedback = ref('')
const showReviseForm = ref(false)
const patchTitle = ref('')
const patchDeadline = ref('')
const patchOwner = ref('')
const patchSecondaries = ref<string[]>([])
const patchAcceptance = ref('')
const fieldDrafts = reactive<Record<string, {
  title: string
  deadline: string
  primary_owner: string
  secondary_owners: string[]
  acceptance_criteria: string
}>>({})
const unsavedNotice = ref<string[]>([])
const unsavedBack = ref<HTMLButtonElement | null>(null)
const ownerConfirmed = reactive<Record<string, boolean>>({})
/** 放行理由：仅当本轮存在阻断错误时才需要，且只随 approve 提交。 */
const overrideReason = ref('')
const operatorClaim = ref('')
const showOverride = ref(false)
/** 刷新失败时的可重试入口：既有快照不清空，只提示并可再次查询。 */
const needRetry = ref(false)
/** App 已经认下的 run_id；跨 run 的旧响应一律拒收。 */
let expectedRunId: string | null = null
const loading = ref(false)
const errorMsg = ref('')
const notice = ref('')
const threadId = ref<string | null>(null)
const locateHint = ref('')
const expand = ref(false)
const sourceMode = ref<'edit' | 'read'>('edit')
const quoteRange = ref<{ start: number; end: number } | null>(null)
const sourcePane = ref<{ scrollToMark: () => void } | null>(null)
const restoring = ref(false)
const sourceOpen = ref(false)
const narrow = ref(typeof window !== 'undefined' && window.matchMedia('(max-width: 960px)').matches)
const resultView = ref<'list' | 'people' | 'markdown'>('list')
const resultTabs = ['list', 'people', 'markdown'] as const
let narrowQuery: MediaQueryList | null = null
let runSeq = 0
let abortCtrl: AbortController | null = null
/** 轮询代次：同一时刻只允许一个 poller，旧 poller 每轮自查后自行退出。 */
let pollerSeq = 0
const POLL_DELAYS = [500, 1000, 2000, 4000]
const SOFT_LIMIT = 8000
const HARD_LIMIT = 100000

const sampleButtons = [
  { name: '示例1', path: '/samples/demo_sprint_planning.txt' },
  { name: '示例2', path: '/samples/demo_incident_review.txt' },
  { name: '示例3', path: '/samples/demo_feature_review.txt' },
]

const taskItems = computed(() => snapshot.value?.task_draft?.items ?? [])
const stakeholders = computed(() => snapshot.value?.meeting_brief?.stakeholders ?? [])
const markdownPreview = computed(() => snapshot.value?.markdown_output ?? '')
const mdView = ref<'preview' | 'source'>('preview')
const phase = computed(() => viewPhase({
  restoring: restoring.value,
  interruptType: String(interrupt.value?.type || ''),
  loading: loading.value,
  status: snapshot.value?.status,
}))
const stageSteps = ['录入', '生成', '确认', '完成']
const stageIndex = computed(() => {
  const map = { intake: 0, restore: 1, running: 1, clarify: 1, assign: 2, review: 2, done: 3, failed: -1 }
  return map[phase.value]
})
const showSource = computed(() => {
  if (phase.value === 'running' || phase.value === 'restore') return false
  if (phase.value === 'done') return sourceOpen.value
  return true
})
const layoutClass = computed(() => {
  const split = phase.value === 'assign'
    || phase.value === 'review'
    || (phase.value === 'done' && sourceOpen.value)
  return split ? 'layout-split' : 'layout-stack'
})
const sortedReviewItems = computed(() =>
  reviewOrder(reviewItems.value, snapshot.value?.validation?.issues ?? [], (item) => effectivePrimary(item)),
)
const looseIssues = computed(() =>
  (snapshot.value?.validation?.issues ?? []).filter((issue) => !issue.item_id),
)
const LIVE: Record<string, string> = {
  intake: '正在读取会议原文',
  clarify: '正在处理补充的会议目标',
  decompose: '正在整理任务',
  validate_repair: '正在检查任务是否完整',
  propose_owners: '正在整理负责人建议',
  assign_owners: '正在整理负责人',
  review: '正在整理待审核任务',
  finalize: '正在整理可以导出的结果',
}
const liveCopy = computed(() => {
  if (notice.value) return notice.value
  const id = PIPELINE_IDS.find((node) => nodeStatus[node] === 'running')
    || PIPELINE_IDS.find((node) => nodeStatus[node] === 'waiting')
  if (id && LIVE[id]) return LIVE[id]
  return '正在等待服务端返回进度'
})

const markdownHtml = computed(() => {
  if (!markdownPreview.value) return ''
  const html = marked.parse(markdownPreview.value, { async: false }) as string
  return DOMPurify.sanitize(html)
})
const assignItems = computed(() => (interrupt.value?.items as Array<Record<string, unknown>>) ?? [])

/** 阻断错误：非空时「一键批准」必须先写放行理由。 */
const blockingErrors = computed<ProblemIssue[]>(() => {
  const raw = interrupt.value?.blocking_errors
  if (!Array.isArray(raw)) return []
  return (raw as ProblemIssue[]).filter((row) => row && typeof row === 'object')
})

const needOverride = computed(() => {
  const advertised = interrupt.value?.override_required
  return typeof advertised === 'boolean' ? advertised : blockingErrors.value.length > 0
})

/** interrupt 上的抽取情况优先，没有时退回快照。 */
const extraction = computed<Extraction | null>(() => {
  const fromInterrupt = interrupt.value?.extraction
  if (fromInterrupt && typeof fromInterrupt === 'object') return fromInterrupt as Extraction
  return snapshot.value?.extraction ?? null
})

const extractionPartial = computed(() => Boolean(extraction.value?.partial || extraction.value?.degraded))

const extractionWarning = computed(() => {
  if (!extractionPartial.value) return ''
  const failed = Number(extraction.value?.chunks_failed ?? 0)
  if (failed > 0) {
    return `本次有 ${failed} 个分块抽取失败，结果可能不完整，请人工确认。`
  }
  // 没有失败但降级：通常是模型不可用，只走了确定性规则。
  if (extraction.value?.degraded) return '本次仅规则结果，抽取能力受限，请人工确认。'
  return '本次抽取不完整，结果可能不完整，请人工确认。'
})

const extractionDetail = computed(() => String(extraction.value?.detail || '').trim())
const reviewItems = computed(() => {
  const rows = (interrupt.value?.items as Array<Record<string, unknown>>) ?? []
  const byId = new Map(taskItems.value.map((item) => [item.id, item]))
  return rows.map((row) => overlayReviewItem(row, byId.get(String(row.id || ''))))
})
const currentReview = computed(() =>
  reviewItems.value.find((item) => String(item.id || '') === reviseItemId.value) || null,
)
const showLog = computed(() =>
  (progressLog.value.length > 0 || toolLog.value.length > 0)
  && (phase.value === 'intake' || phase.value === 'running' || phase.value === 'failed'),
)

function effectivePrimary(item: { id?: unknown; primary_owner?: unknown }) {
  const id = String(item.id || '')
  if (showReviseForm.value && id === reviseItemId.value) return patchOwner.value
  if (fieldDrafts[id]) return fieldDrafts[id].primary_owner
  if (assignDrafts[id]) return assignDrafts[id].primary_owner
  return String(item.primary_owner || '')
}

function itemIssues(itemId: string) {
  return (snapshot.value?.validation?.issues ?? []).filter((issue) => (issue.item_id || '') === itemId)
}

function fieldIssues(itemId: string, slot: IssueSlot) {
  return itemIssues(itemId).filter((issue) => issueField(issue.field) === slot)
}

function labelsFor(item: { id?: unknown; primary_owner?: unknown }) {
  return confirmLabels(itemIssues(String(item.id || '')), effectivePrimary(item))
}

const confirmStats = computed(() => {
  let fix = 0
  let owner = 0
  let advise = 0
  let overlap = 0
  for (const item of reviewItems.value) {
    const labels = labelsFor(item)
    if (labels.includes('需修改')) fix += 1
    if (labels.includes('待分配负责人')) owner += 1
    if (labels.includes('建议补充')) advise += 1
    if (labels.includes('需修改') && labels.includes('待分配负责人')) overlap += 1
  }
  return { total: reviewItems.value.length, fix, owner, advise, overlap }
})

function syncOwnerDraft() {
  const draft = assignDrafts[reviseItemId.value]
  if (!draft) return
  draft.primary_owner = patchOwner.value
  draft.secondary_owners = patchSecondaries.value.filter((name) => name !== patchOwner.value)
}

function issueTone(severity: string) {
  return severity === 'error' ? '需修改' : '建议补充'
}

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
  } else {
    draft.primary_owner = cand.name
    draft.secondary_owners = draft.secondary_owners.filter(n => n !== cand.name)
  }
  if (itemId === reviseItemId.value) {
    patchOwner.value = draft.primary_owner
    patchSecondaries.value = [...draft.secondary_owners]
  }
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
  // 用 interrupt 里已有的主责初始化；建议不预填 select，避免「建议被当成已写入」。
  // 已有草稿不再覆盖，否则刷新会把用户清空的负责人填回服务端原值。
  const items = (payload.items as Array<Record<string, unknown>>) || []
  for (const item of items) {
    const id = String(item.id || '')
    if (!id || assignDrafts[id]) continue
    assignDrafts[id] = {
      primary_owner: String(item.primary_owner || ''),
      secondary_owners: Array.isArray(item.secondary_owners)
        ? (item.secondary_owners as string[])
        : [],
    }
    ownerConfirmed[id] = false
  }
}

async function loadSample(path: string) {
  try {
    const resp = await fetch(path)
    // fetch 对 404 也 resolve，必须显式判 ok，否则会把错误页正文当原文。
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    sourceText.value = await resp.text()
  } catch {
    errorMsg.value = '无法加载示例文件，请手动粘贴'
  }
}

function makeHandlers(mine: () => boolean, seq: number) {
  // 每个入口自己的闭包。旧流迟到回调看到代次不对就丢掉，避免关掉新一次的 loading。
  return {
    onMetadata: (d: Record<string, unknown>) => {
      if (!mine()) return
      if (d.thread_id) {
        threadId.value = d.thread_id as string
        rememberThread(threadId.value)
      }
      // 新一轮开始后旧 run_id 作废，等结果里带上新 run_id 再认下来。
      if (d.run_id) expectedRunId = String(d.run_id)
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
      // 中断同样要过快照门禁：旧流迟到的 interrupt 不能把完成态改回审核态。
      if (!acceptInterrupt(d, seq)) return
      const t = String(d.type || '')
      if (PIPELINE_IDS.includes(t)) nodeStatus[t] = 'waiting'
    },
    onResult: (d: Record<string, unknown>) => {
      if (!mine()) return
      applySnapshot(d, seq)
    },
    onError: (d: { message: string; code?: string }) => {
      if (!mine()) return
      errorMsg.value = d.message
      if (d.code === 'SNAPSHOT_CONFLICT' || d.code === 'LEASE_LOST') {
        // 这条流没有权威结果：废掉旧轮询与 run 门禁，只接收重新读取的服务端快照。
        pollerSeq += 1
        expectedRunId = null
        interrupt.value = null
        loading.value = false
        void refreshThread(seq)
      }
    },
    onDone: () => {
      if (!mine()) return
      loading.value = false
    },
  }
}

/**
 * 中断帧也要过快照门禁。
 * interrupt 事件不带完整快照，但它会决定页面显示「分配/审核」面板，所以不能绕过
 * acceptSnapshot 直接写 interrupt.value。这里用当前快照补齐缺失标识后再判一次，
 * 并按状态与中断标识排掉旧帧：完成/失败页面上迟到的 interrupt 一律丢弃。
 *
 * 后端会在 interrupt payload 里补 thread_id / run_id / revision，但旧缓存和
 * 回放流可能没有这些字段，所以缺字段时不强依赖，只按「当前是否仍在等待人工操作」判断。
 */
function acceptInterrupt(data: Record<string, unknown>, seq: number): boolean {
  const type = String(data.type || '')
  // 只认三种人工中断；其余类型不会改页面的等待状态。
  if (!HITL_NODES.has(type)) return false

  const current = snapshot.value
  const thread = String(data.thread_id ?? current?.thread_id ?? threadId.value ?? '')
  if (threadId.value && thread && thread !== threadId.value) return false
  const run = String(data.run_id ?? current?.run_id ?? expectedRunId ?? '')
  if (expectedRunId && run && run !== expectedRunId) return false

  const incomingRevision = Number(data.revision ?? Number.NaN)
  const currentRevision = Number(current?.revision ?? Number.NaN)
  if (Number.isFinite(incomingRevision) && Number.isFinite(currentRevision) && incomingRevision < currentRevision) {
    return false
  }

  // 只有服务端真的停在待审核时，中断才代表「现在要人操作」。完成/失败/运行中的页面上
  // 收到的 interrupt 都是旧流迟到的帧，不能把审核面板重新拉起来。
  // 真流程里 interrupt 帧会紧跟一条 result，由 applySnapshot 统一落状态；这里丢掉不影响。
  if (current?.status && current.status !== 'awaiting_review') return false

  const incomingId = String(data.interrupt_id ?? '')
  const probe = {
    thread_id: thread || null,
    run_id: run || null,
    status: 'awaiting_review',
    revision: Number.isFinite(incomingRevision)
      ? incomingRevision
      : (Number.isFinite(currentRevision) ? currentRevision : 0),
    interrupt_id: incomingId,
    interrupt_payload: data,
  }
  const verdict = acceptSnapshot(current, probe, {
    seq,
    currentSeq: runSeq,
    threadId: threadId.value,
    runId: expectedRunId,
  })
  if (!verdict.ok) return false
  if (!nextInterrupt(probe)) return false

  // 快照仍停在另一个中断上：只有能证明这一帧更新（revision 更大）才替换，
  // 否则等随后的 result 帧统一收口，避免旧流把审核面板换成过期内容。
  const currentId = String(current?.interrupt_id ?? '')
  if (current && current.status === 'awaiting_review' && currentId && incomingId && currentId !== incomingId) {
    const newer = Number.isFinite(incomingRevision) && Number.isFinite(currentRevision)
      && incomingRevision > currentRevision
    if (!newer) return false
  }

  interrupt.value = data
  return true
}

/**
 * 服务端快照统一入口：SSE 的 result 和刷新线程都走这里。
 * 先过 acceptSnapshot，拒绝旧 seq / 别的 run / 倒退的 revision，
 * 迟到的旧响应才不会把页面改回上一次的状态。
 */
function applySnapshot(data: Record<string, unknown>, seq: number): boolean {
  const next = data as unknown as Snapshot
  const verdict = acceptSnapshot(snapshot.value, next, {
    seq,
    currentSeq: runSeq,
    threadId: threadId.value,
    runId: expectedRunId,
  })
  if (!verdict.ok) return false
  snapshot.value = next
  expectedRunId = String(next.run_id || '') || null
  needRetry.value = false
  // 只有服务端真的停在待审核时才有中断可操作，其余状态一律清空。
  const pending = nextInterrupt(next)
  interrupt.value = pending
  if (pending) seedAssignDrafts(pending)
  if (next.source_text && !sourceText.value) sourceText.value = next.source_text
  return true
}

function beginRun() {
  const outcome = { result: false, conflict: false }
  const { mine, signal } = startControlledRun()
  const seq = runSeq
  const handlers = makeHandlers(mine, seq)
  const markResult = handlers.onResult
  handlers.onResult = (data) => {
    outcome.result = true
    markResult?.(data)
  }
  const markError = handlers.onError
  handlers.onError = (data) => {
    if (data.code === 'SNAPSHOT_CONFLICT' || data.code === 'LEASE_LOST') outcome.conflict = true
    markError?.(data)
  }
  return { mine, signal, seq, handlers, outcome }
}

async function settleRun(mine: () => boolean, seq: number, outcome: { result: boolean; conflict: boolean }) {
  if (mine()) loading.value = false
  if (!mine() || outcome.conflict || !needsSnapshotRefresh(outcome.result)) return
  await refreshThread(seq)
  if (mine() && shouldFollowPlanning(snapshot.value?.status)) void pollThread(seq)
}

function startControlledRun() {
  const seq = ++runSeq
  abortCtrl?.abort()
  const ctrl = new AbortController()
  abortCtrl = ctrl
  return { mine: () => seq === runSeq, signal: ctrl.signal }
}

function cancelRun() {
  const seq = ++runSeq
  abortCtrl?.abort()
  abortCtrl = null
  loading.value = false
  notice.value = '已停止等待。服务端会按检查点记下当前状态。'
  void pollThread(seq)
}

/**
 * 提交或刷新失败时，用保留下来的快照还原中断。
 * 中断在发起提交时被清空过，不还原的话审核面板会整块消失，用户连重试入口都看不到。
 */
function keepInterrupt() {
  interrupt.value = nextInterrupt(snapshot.value)
}

/**
 * 刷新线程。失败绝不清空既有快照：先保留上次结果，再给可重试入口。
 * 返回 true 表示拿到了被接受的新快照。
 */
async function refreshThread(seq: number): Promise<boolean> {
  const id = threadId.value
  if (!id || !acceptResponse(seq, runSeq)) return false
  let resp: Response
  try {
    resp = await fetch(`/api/v1/threads/${id}`)
  } catch (err) {
    // 主动取消不算故障，别用错误提示打扰用户。
    if (err instanceof DOMException && err.name === 'AbortError') return false
    if (!acceptResponse(seq, runSeq)) return false
    notice.value = '网络异常，已保留上次结果'
    needRetry.value = true
    keepInterrupt()
    return false
  }
  if (!acceptResponse(seq, runSeq)) return false
  if (!resp.ok) {
    notice.value = '已保留上次结果'
    errorMsg.value = `HTTP ${resp.status}`
    needRetry.value = true
    keepInterrupt()
    return false
  }
  let data: Record<string, unknown>
  try {
    data = await resp.json()
  } catch {
    if (!acceptResponse(seq, runSeq)) return false
    notice.value = '已保留上次结果'
    errorMsg.value = '响应格式异常'
    needRetry.value = true
    keepInterrupt()
    return false
  }
  if (!acceptResponse(seq, runSeq)) return false
  return applySnapshot(data, seq)
}

/** 单例守卫：连续点「再次查询」只保留最后一个 poller。 */
async function pollThread(seq: number) {
  const mine = ++pollerSeq
  try {
    for (let i = 0; i < 8; i += 1) {
      if (mine !== pollerSeq || !acceptResponse(seq, runSeq)) return
      await refreshThread(seq)
      if (mine !== pollerSeq || !acceptResponse(seq, runSeq)) return
      if (isSettledStatus(snapshot.value?.status)) {
        notice.value = ''
        needRetry.value = false
        return
      }
      const delay = POLL_DELAYS[Math.min(i, POLL_DELAYS.length - 1)]
      await new Promise((resolve) => setTimeout(resolve, delay))
    }
    if (mine === pollerSeq && acceptResponse(seq, runSeq)) notice.value = '服务端仍在处理'
  } finally {
    if (mine === pollerSeq) pollerSeq = 0
  }
}

/** 「再次查询」：先废掉在跑的 poller，再起新的一个。 */
function requery() {
  // 自增一次即让在跑的 poller 在下一轮自查时退出，新 poller 会拿到更大的代次。
  pollerSeq += 1
  needRetry.value = false
  errorMsg.value = ''
  void pollThread(runSeq)
}

onMounted(async () => {
  narrowQuery = window.matchMedia('(max-width: 960px)')
  narrow.value = narrowQuery.matches
  narrowQuery.addEventListener('change', onNarrow)
  window.addEventListener('keydown', onEsc)
  const saved = rememberedThread()
  if (!saved) return
  restoring.value = true
  threadId.value = saved
  try {
    await refreshThread(runSeq)
    if (shouldFollowPlanning(snapshot.value?.status)) void pollThread(runSeq)
  } finally {
    restoring.value = false
  }
})

async function runWorkflow(clarify?: string) {
  // 澄清走 /chat/stream 而不是 /resume，因为 clarify interrupt 绑在同一 graph_ns
  const pendingInterrupt = interrupt.value
  const { mine, signal, seq, handlers, outcome } = beginRun()
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
    clearFieldDrafts()
    for (const key of Object.keys(assignDrafts)) delete assignDrafts[key]
    for (const key of Object.keys(ownerConfirmed)) delete ownerConfirmed[key]
  }

  try {
    await streamSSE('/api/v1/chat/stream', {
      source_text: sourceText.value,
      thread_id: threadId.value,
      request_id: uuid(),
      clarify_response: clarify || null,
      interrupt_id: clarify ? String(pendingInterrupt?.interrupt_id || '') : null,
    }, handlers, signal)
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    await settleRun(mine, seq, outcome)
  }
}

async function resume(approve = false) {
  // 分配必须带 assignments 且 approve=false，避免误走「一键批准」分支
  if (!snapshot.value?.run_id) return
  const pending = interrupt.value
  const originals = ownerOriginals(pending?.items as Array<{ id?: string; primary_owner?: string }> | undefined)
  const { mine, signal, seq, handlers, outcome } = beginRun()
  loading.value = true
  errorMsg.value = ''
  notice.value = ''
  const type = String(pending?.type || 'review')
  interrupt.value = null
  showReviseForm.value = false

  const body: Record<string, unknown> = {
    request_id: uuid(),
    interrupt_type: type,
    interrupt_id: String(pending?.interrupt_id || ''),
    approve,
    revision_scope: reviseScope.value,
    target_item_id: reviseScope.value === 'document' ? null : (reviseTargetId.value || null),
    feedback: reviseFeedback.value,
  }
  if (reviseScope.value === 'drop' && reviseTargetId.value) {
    body.drop_item_ids = [reviseTargetId.value]
  }
  if (type === 'assign_owners' || (type === 'review' && approve)) {
    body.assignments = Object.entries(assignDrafts).map(([item_id, draft]) => ({
      item_id,
      primary_owner: draft.primary_owner,
      secondary_owners: draft.secondary_owners,
      confirm_owner: confirmOwner(Boolean(ownerConfirmed[item_id]), draft.primary_owner, originals[item_id] || ''),
    }))
    if (type === 'assign_owners') body.approve = false
  }
  if (approve) {
    // 放行理由只随批准提交；分配/修订走自己的语义，不带 override 字段。
    const reason = overrideReason.value.trim()
    const claim = operatorClaim.value.trim()
    if (reason) body.override_reason = reason
    if (claim) body.operator_claim = claim
  }

  try {
    await streamSSE(`/api/v1/runs/${snapshot.value.run_id}/resume`, body, handlers, signal)
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    await settleRun(mine, seq, outcome)
    // 提交成功的放行理由不再复用，避免下一次批准带上次的旧理由。
    if (mine() && approve && !errorMsg.value) {
      overrideReason.value = ''
      operatorClaim.value = ''
      showOverride.value = false
    }
  }
}

async function saveFieldPatch() {
  if (!snapshot.value?.run_id || !reviseItemId.value) return
  const savedId = reviseItemId.value
  const pending = interrupt.value
  const { mine, signal, seq, handlers, outcome } = beginRun()
  loading.value = true
  errorMsg.value = ''
  interrupt.value = null
  try {
    await streamSSE(`/api/v1/runs/${snapshot.value.run_id}/resume`, {
      request_id: uuid(),
      interrupt_type: String(pending?.type || 'review'),
      interrupt_id: String(pending?.interrupt_id || ''),
      approve: false,
      revision_scope: 'item',
      target_item_id: savedId,
      feedback: '',
      item_patch: {
        title: patchTitle.value,
        deadline: patchDeadline.value,
        primary_owner: patchOwner.value,
        secondary_owners: patchSecondaries.value.filter(name => name !== patchOwner.value),
        acceptance_criteria: patchAcceptance.value,
      },
    }, handlers, signal)
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    await settleRun(mine, seq, outcome)
    if (mine() && savedId && !errorMsg.value) delete fieldDrafts[savedId]
  }
}

async function resumeDrop(itemId: string) {
  // 审核面板「不是任务」：显式 drop，不依赖自然语言解析
  if (!snapshot.value?.run_id) return
  const pending = interrupt.value
  const { mine, signal, seq, handlers, outcome } = beginRun()
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
        interrupt_id: String(pending?.interrupt_id || ''),
        approve: false,
        revision_scope: 'drop',
        drop_item_ids: [itemId],
        feedback: '',
      },
      handlers,
      signal,
    )
  } catch (err) {
    if (mine()) errorMsg.value = err instanceof Error ? err.message : '请求失败'
  } finally {
    await settleRun(mine, seq, outcome)
  }
}

function togglePatchSecondary(name: string) {
  const set = new Set(patchSecondaries.value)
  if (set.has(name)) set.delete(name)
  else set.add(name)
  patchSecondaries.value = [...set].filter(item => item !== patchOwner.value)
  syncOwnerDraft()
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

async function download(format: 'md' | 'json') {
  const id = threadId.value
  if (!id) return
  // window.open 感知不到 HTTP 失败（404/500 也会开一个白页），改成取回 Blob 再触发下载。
  try {
    const resp = await fetch(`/api/v1/threads/${id}/export?format=${format}`)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `specops-${id}.${format}`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  } catch (err) {
    errorMsg.value = `导出失败：${err instanceof Error ? err.message : '未知错误'}`
  }
}

function asBoardItem(raw: Record<string, unknown> | BoardItem): BoardItem {
  const item = raw as BoardItem & { description?: string }
  return {
    id: String(item.id || ''),
    title: String(item.title || ''),
    description: String(item.description || ''),
    primary_owner: String(item.primary_owner || ''),
    secondary_owners: Array.isArray(item.secondary_owners) ? item.secondary_owners.map(String) : [],
    evidence_quote: String(item.evidence_quote || ''),
    unresolved_ref: String(item.unresolved_ref || ''),
    deadline: String(item.deadline || ''),
    acceptance_criteria: String(item.acceptance_criteria || ''),
    acceptance_origin: String(item.acceptance_origin || ''),
    evidence_kind: String(item.evidence_kind || ''),
    inference_reason: String(item.inference_reason || ''),
  }
}

function clearFieldDrafts() {
  unsavedNotice.value = []
  for (const key of Object.keys(fieldDrafts)) delete fieldDrafts[key]
}

function serverFields(item: BoardItem) {
  const owner = assignDrafts[item.id]
  return {
    title: item.title || '',
    deadline: item.deadline || '',
    primary_owner: owner ? owner.primary_owner : (item.primary_owner || ''),
    secondary_owners: [...(owner ? owner.secondary_owners : item.secondary_owners || [])],
    acceptance_criteria: item.acceptance_criteria || '',
  }
}

function stashCurrent() {
  const id = reviseItemId.value
  if (!id || !showReviseForm.value) return
  fieldDrafts[id] = {
    title: patchTitle.value,
    deadline: patchDeadline.value,
    primary_owner: patchOwner.value,
    secondary_owners: [...patchSecondaries.value],
    acceptance_criteria: patchAcceptance.value,
  }
}

function loadDraft(item: BoardItem) {
  const next = resolveDraft(fieldDrafts[item.id], serverFields(item))
  patchTitle.value = next.title
  patchDeadline.value = next.deadline
  patchOwner.value = next.primary_owner
  patchSecondaries.value = [...next.secondary_owners]
  patchAcceptance.value = next.acceptance_criteria
  syncOwnerDraft()
}

function unsavedByItem(raw: Record<string, unknown> | BoardItem) {
  const item = asBoardItem(raw)
  const live = String(raw.id || '') === reviseItemId.value && showReviseForm.value
  const current = live
    ? { title: patchTitle.value, deadline: patchDeadline.value, acceptance: patchAcceptance.value }
    : fieldDrafts[item.id]
      ? {
          title: fieldDrafts[item.id].title,
          deadline: fieldDrafts[item.id].deadline,
          acceptance: fieldDrafts[item.id].acceptance_criteria,
        }
      : null
  if (!current) return []
  return fieldChanges(current, {
    title: item.title,
    deadline: item.deadline || '',
    acceptance: item.acceptance_criteria || '',
  })
}

function unsavedLines() {
  return reviewItems.value.flatMap((raw) => {
    const fields = unsavedByItem(raw)
    if (!fields.length) return []
    const title = String(raw.title || '未命名任务')
    return [`${title}：${fields.join('、')}`]
  })
}

/**
 * 放行理由必填：存在阻断错误时，理由 trim 后为空就不提交，改为展开理由表单。
 * 没填理由的批准按契约会被后端 422 拒绝，前端先拦住才不会白跑一趟。
 */
function overrideReady(): boolean {
  if (!needOverride.value) return true
  if (overrideReason.value.trim()) return true
  showOverride.value = true
  void nextTick(() => document.getElementById('override-reason')?.focus())
  return false
}

function requestApprove() {
  if (!overrideReady()) return
  const lines = unsavedLines()
  if (lines.length) {
    unsavedNotice.value = lines
    void nextTick(() => unsavedBack.value?.focus())
    return
  }
  void resume(true)
}

function discardUnsavedAndApprove() {
  if (!overrideReady()) return
  clearFieldDrafts()
  void resume(true)
}

function viewCurrentSource() {
  if (!currentReview.value) return
  sourceOpen.value = true
  void locateInSource(asBoardItem(currentReview.value))
}

function quoteInSource(quote: unknown) {
  const text = String(quote || '').trim()
  return Boolean(text) && sourceText.value.includes(text)
}

function onResultTabKey(event: KeyboardEvent) {
  const next = moveTabIndex(resultTabs.indexOf(resultView.value), resultTabs.length, event.key)
  if (next === null) return
  event.preventDefault()
  resultView.value = resultTabs[next]
  void nextTick(() => document.getElementById(`result-tab-${resultView.value}`)?.focus())
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
  sourcePane.value?.scrollToMark()
}

function editSource() {
  sourceMode.value = 'edit'
}

function selectItemForRevise(item: BoardItem) {
  // 再次点中同一项只定位原文，不把表单刷回服务端原值。
  if (item.id === reviseItemId.value && showReviseForm.value) {
    void locateInSource(item)
    return
  }
  stashCurrent()
  reviseItemId.value = item.id
  reviseTargetId.value = item.id
  reviseScope.value = 'item'
  showReviseForm.value = true
  loadDraft(item)
  void locateInSource(item)
  void nextTick(() => {
    document.getElementById(`task-${item.id}`)?.scrollIntoView({ block: 'start' })
  })
}

function focusHitlItem(raw: Record<string, unknown>) {
  // 分配面板里的定位入口：选中该项并把原文面板打开，窄屏下面板是抽屉，
  // 不打开就只标了 <mark> 而用户看不到，所以这里和文案承诺保持一致。
  const item = asBoardItem(raw)
  reviseItemId.value = item.id
  sourceOpen.value = true
  void locateInSource(item)
}

function locateHitlItem(raw: Record<string, unknown>, ev?: Event) {
  // 只有点标题区才定位；select/button 点击会被吞掉，避免抢走下拉焦点
  if (isHitlInteractiveTarget(ev?.target ?? null)) return
  focusHitlItem(raw)
}

function onEsc(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (expand.value) expand.value = false
  else if (sourceOpen.value) sourceOpen.value = false
}

function onNarrow(event: MediaQueryListEvent) {
  narrow.value = event.matches
}

/** 只有在等人工操作的阶段，窄屏抽屉才需要锁整页滚动；完成页看原文不该把页面钉住。 */
const isHitlPhase = computed(() => ['clarify', 'assign', 'review'].includes(phase.value))
/** 放大原文始终是模态；窄屏抽屉只在 HITL 阶段锁滚动。 */
const lockScroll = computed(() => expand.value || (sourceOpen.value && narrow.value && isHitlPhase.value))

watch(lockScroll, (on) => {
  document.body.style.overflow = on ? 'hidden' : ''
})

watch(needOverride, (on) => {
  // 一旦出现阻断错误就默认摊开理由表单，避免用户对着灰按钮找不到原因。
  if (on) showOverride.value = true
})

watch(phase, (next) => {
  if (next === 'review' || next === 'assign' || next === 'done' || next === 'clarify') sourceMode.value = 'read'
  if (next === 'intake' || next === 'failed') sourceMode.value = 'edit'
  // 离开等待/编辑阶段后抽屉不再有意义，收起来以免挡住新的主操作区。
  if (next === 'intake' || next === 'running' || next === 'done' || next === 'failed') sourceOpen.value = false
})

/** 抽屉关闭后把焦点还给触发它的按钮；抽屉的打开按钮在 App 侧，SourcePane 拿不到。 */
let drawerTrigger: HTMLElement | null = null
function openDrawer() {
  const active = document.activeElement
  drawerTrigger = active instanceof HTMLElement ? active : null
  sourceOpen.value = true
}

watch(sourceOpen, async (open) => {
  if (open) {
    await nextTick()
    sourcePane.value?.scrollToMark()
    return
  }
  // 收起后 Teleport 结束，等 DOM 稳定再归还焦点，否则会被搬动覆盖。
  await nextTick()
  if (drawerTrigger?.isConnected) drawerTrigger.focus()
  drawerTrigger = null
})

onUnmounted(() => {
  window.removeEventListener('keydown', onEsc)
  narrowQuery?.removeEventListener('change', onNarrow)
  document.body.style.overflow = ''
  abortCtrl?.abort()
})

resetPipeline()
</script>
<template>
  <div class="shell" :class="[layoutClass, 'phase-' + phase]">
    <header class="top">
      <p class="product">会议任务工作台</p>
      <ol class="stages">
        <li
          v-for="(step, index) in stageSteps"
          :key="step"
          :class="{ current: index === stageIndex, done: stageIndex > index }"
          :aria-current="index === stageIndex ? 'step' : undefined"
        >
          <span>{{ step }}</span>
          <span v-if="index === stageIndex" class="sr-only">当前阶段</span>
          <span v-else-if="stageIndex > index" class="sr-only">已完成</span>
        </li>
      </ol>
    </header>

    <div v-if="errorMsg" class="banner" role="alert">
      <p>{{ errorMsg }}</p>
      <p>可以修改原文后重新生成，或再次查询当前进度。</p>
      <button v-if="threadId" type="button" class="btn-ghost" @click="requery">再次查询</button>
    </div>

    <div v-else-if="needRetry" class="banner" role="status">
      <p>{{ notice || '已保留上次结果' }}</p>
      <button v-if="threadId" type="button" class="btn-ghost" @click="requery">再次查询</button>
    </div>

    <section v-if="phase === 'intake'" class="lead">
      <h1>贴上会议原文</h1>
      <p>把会议记录贴在下面。生成后，可以核对任务、负责人和原文依据。</p>
      <div class="sample-row">
        <button v-for="sample in sampleButtons" :key="sample.name" type="button" class="btn-ghost" @click="loadSample(sample.path)">
          {{ sample.name }}
        </button>
      </div>
    </section>

    <div v-if="showSource" class="source-slot">
      <SourcePane
        ref="sourcePane"
        v-model:text="sourceText"
        :mode="sourceMode"
        :expanded="expand"
        :drawer="narrow && sourceOpen"
        :open="sourceOpen"
        :parts="quoteParts"
        :hint="narrow && !sourceOpen ? '' : locateHint"
        :soft-limit="SOFT_LIMIT"
        :hard-limit="HARD_LIMIT"
        @edit="editSource"
        @update:expanded="expand = $event"
        @close="sourceOpen = false"
      />
    </div>

    <main class="stage-main">
      <section v-if="phase === 'intake'" class="actions">
        <button
          type="button"
          class="btn-primary"
          :disabled="loading || !sourceText.trim() || sourceText.length > HARD_LIMIT"
          @click="runWorkflow()"
        >
          生成任务
        </button>
      </section>

      <section v-else-if="phase === 'restore'" aria-live="polite">
        <h1>{{ restoring ? '正在恢复上次的审核' : '审核内容还没有载入' }}</h1>
        <p>{{ restoring ? '正在读取服务端保存的进度。' : '服务端仍停在待确认，但这次没有拿到待确认的内容。' }}</p>
        <button v-if="!restoring" type="button" class="btn-ghost" @click="requery">再次查询</button>
      </section>

      <section v-else-if="phase === 'running'" aria-live="polite">
        <h1>正在生成任务</h1>
        <p class="lead-copy">{{ liveCopy }}</p>
        <Pipeline :statuses="nodeStatus" />
        <p class="note">取消会停止这次等待。服务端会按检查点记下当前状态。</p>
        <div class="actions">
          <button v-if="loading" type="button" class="btn-ghost" @click="cancelRun">取消等待</button>
          <button v-if="notice === '服务端仍在处理'" type="button" class="btn-ghost" @click="requery">再次查询</button>
        </div>
      </section>

      <section v-else-if="phase === 'clarify'">
        <h1>补充会议目标</h1>
        <p>{{ interrupt?.message || '还需要一个会议目标，才能继续整理任务。' }}</p>
        <label class="field-label" for="clarify-goal">会议目标</label>
        <input id="clarify-goal" v-model="clarifyInput" />
        <div class="actions">
          <button type="button" class="btn-primary" :disabled="loading || !clarifyInput.trim()" @click="runWorkflow(clarifyInput)">
            提交补充
          </button>
        </div>
      </section>

      <section v-else-if="phase === 'assign'" class="hitl-assign">
        <h1>无法从原文确认主责的任务</h1>
        <p>{{ interrupt?.message || '请为下面的任务指定负责人。点选建议不会直接保存，提交分配后才会写入。' }}</p>
        <p v-if="narrow" class="note">选中任务后，打开原文对照依据。</p>
        <button v-if="narrow" type="button" class="btn-ghost" @click="openDrawer">查看原文</button>
        <div
          v-for="item in assignItems"
          :key="String(item.id)"
          class="assign-row"
          :class="{ selected: reviseItemId === String(item.id) }"
        >
          <div class="assign-locate">
            <h2>
              <button type="button" class="text-button" @click="locateHitlItem(item)">
                {{ item.title }}
                <span v-if="reviseItemId === String(item.id)" class="current-flag">当前选中</span>
              </button>
            </h2>
            <p v-if="item.unresolved_ref">
              原文未点名：{{ item.unresolved_ref }}。
              <button type="button" class="locate-link" @click.stop="focusHitlItem(item)">点击这里定位原文</button>
            </p>
            <p v-if="item.description">{{ item.description }}</p>
            <p v-if="item.evidence_quote">依据：{{ item.evidence_quote }}</p>
          </div>
          <div v-if="assignDrafts[String(item.id)]" class="assign-controls" @click.stop @mousedown.stop>
            <label class="field-label" :for="'owner-' + item.id">负责人</label>
            <label class="check">
              <input v-model="ownerConfirmed[String(item.id)]" type="checkbox" @click.stop />
              <span>确认此姓名</span>
            </label>
            <select
              :id="'owner-' + item.id"
              v-model="assignDrafts[String(item.id)].primary_owner"
              @mousedown.stop
              @click.stop
            >
              <option value="">请选择</option>
              <option v-for="name in assignStakeholders" :key="name" :value="name">{{ name }}</option>
            </select>
            <p class="field-label">次要负责人</p>
            <div class="chip-row">
              <button
                v-for="name in assignStakeholders"
                :key="name"
                type="button"
                class="chip"
                :class="{ on: assignDrafts[String(item.id)]?.secondary_owners.includes(name) }"
                :aria-pressed="assignDrafts[String(item.id)]?.secondary_owners.includes(name)"
                :disabled="assignDrafts[String(item.id)]?.primary_owner === name"
                @click.stop="toggleSecondary(String(item.id), name)"
              >
                {{ name }}
              </button>
            </div>
            <div v-if="itemSuggestions(item).length" class="suggest-block" @click.stop>
              <p class="field-label">建议，仅供参考</p>
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
              <p v-if="itemSuggestions(item)[0]?.reason" class="note">{{ itemSuggestions(item)[0].reason }}</p>
            </div>
          </div>
        </div>
        <div class="actions approve-bar">
          <button type="button" class="btn-ghost" :disabled="loading || !canAdoptAll" @click="adoptAllSuggestions">采纳全部</button>
          <button type="button" class="btn-primary" :disabled="loading" @click="resume(false)">提交分配</button>
        </div>
      </section>

      <section v-else-if="phase === 'review'" class="hitl-review">
        <h1>审核任务清单</h1>
        <p>{{ interrupt?.message || '核对任务后批准，或先改某一项。' }}</p>
        <p class="note">{{ confirmHeadline(confirmStats) }}序号是全部任务的顺序。选中一项后，原文会标出依据。</p>
        <button v-if="narrow" type="button" class="btn-ghost" @click="openDrawer">查看原文</button>
        <div v-if="extractionWarning" class="banner banner-warn" role="status">
          <p>{{ extractionWarning }}</p>
          <p v-if="extractionDetail" class="note">{{ extractionDetail }}</p>
        </div>
        <div v-if="needOverride" class="issue-list issue-blocking" role="alert">
          <p><strong>有 {{ blockingErrors.length }} 项必须先处理或显式放行。</strong>直接批准会被服务端拒绝。</p>
          <div v-for="(issue, idx) in blockingErrors" :key="String(issue.code) + idx" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
            <details><summary>标识</summary><span class="mono">{{ issue.code }}</span></details>
          </div>
        </div>
        <div v-if="looseIssues.length" class="issue-list">
          <div v-for="issue in looseIssues" :key="issue.code + issue.message" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
            <details><summary>标识</summary><span class="mono">{{ issue.code }}</span></details>
          </div>
        </div>
        <article
          v-for="item in sortedReviewItems"
          :id="'task-' + item.id"
          :key="String(item.id)"
          class="task-card"
          :class="{ selected: reviseItemId === String(item.id) }"
          @click="selectItemForRevise(asBoardItem(item))"
        >
          <div class="row">
            <button
              type="button"
              class="text-button"
              :aria-expanded="reviseItemId === String(item.id)"
              :aria-controls="'editor-' + item.id"
              @click.stop="selectItemForRevise(asBoardItem(item))"
            >
              <span class="index">{{ item.index }}.</span>
              {{ item.title }}
              <span v-if="reviseItemId === String(item.id)" class="current-flag">当前选中</span>
            </button>
            <button type="button" class="btn-ghost" :disabled="loading" @click.stop="resumeDrop(String(item.id))">删除</button>
          </div>
          <p>负责人：{{ effectivePrimary(item) || '待分配负责人' }}</p>
          <p v-if="item.deadline">截止日期：{{ item.deadline }}</p>
          <p v-if="labelsFor(item).length || unsavedByItem(item).length" class="flag-row">
            <span v-for="label in labelsFor(item)" :key="label" class="wait-flag">{{ label }}</span>
            <span v-if="unsavedByItem(item).length" class="wait-flag">未保存：{{ unsavedByItem(item).join('、') }}</span>
          </p>
          <p v-if="item.acceptance_origin === 'system'">验收标准是系统建议</p>
          <p v-if="item.evidence_kind === 'inference'">推断<span v-if="item.inference_reason">：{{ item.inference_reason }}</span></p>

        <form
          v-if="showReviseForm && currentReview && reviseItemId === String(item.id)"
          :id="'editor-' + item.id"
          class="editor"
          @submit.prevent="saveFieldPatch"
          @click.stop
        >
          <h2>修改这项</h2>
          <p v-if="narrow && !sourceOpen && locateHint" class="locate-hint" role="status">{{ locateHint }}</p>
          <div v-for="issue in fieldIssues(String(currentReview.id), 'general')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
            <details><summary>标识</summary><span class="mono">{{ issue.code }}</span></details>
          </div>

          <label class="field-label" for="patch-title">标题</label>
          <input id="patch-title" v-model="patchTitle" />
          <div v-for="issue in fieldIssues(String(currentReview.id), 'title')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
          </div>

          <label class="field-label" for="patch-owner">负责人</label>
          <input id="patch-owner" v-model="patchOwner" list="stakeholder-names" @input="syncOwnerDraft" />
          <datalist id="stakeholder-names">
            <option v-for="name in assignStakeholders" :key="name" :value="name" />
          </datalist>
          <div v-for="issue in fieldIssues(String(currentReview.id), 'owner')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
          </div>

          <p class="field-label">次要负责人</p>
          <div class="chip-row">
            <button
              v-for="name in assignStakeholders"
              :key="'patch-' + name"
              type="button"
              class="chip"
              :class="{ on: patchSecondaries.includes(name) }"
              :aria-pressed="patchSecondaries.includes(name)"
              @click="togglePatchSecondary(name)"
            >
              {{ name }}
            </button>
          </div>
          <div v-for="issue in fieldIssues(String(currentReview.id), 'secondary')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
          </div>
          <div v-if="itemSuggestions(currentReview).length" class="suggest-block">
            <p class="field-label">建议，仅供参考。点选后仍须批准才会写入。</p>
            <div class="chip-row">
              <button
                v-for="(cand, idx) in itemSuggestions(currentReview)"
                :key="String(currentReview.id) + cand.name + idx"
                type="button"
                class="suggest-chip"
                :class="{
                  on: cand.role === 'secondary'
                    ? patchSecondaries.includes(cand.name)
                    : patchOwner === cand.name,
                }"
                @click="applySuggestion(String(currentReview.id), cand)"
              >
                <span>{{ cand.name }}</span>
                <span class="suggest-band">{{ cand.role === 'secondary' ? '次责' : '主责' }} · {{ confidenceBand(Number(cand.confidence) || 0) }}</span>
              </button>
            </div>
          </div>

          <label class="field-label" for="patch-deadline">截止日期</label>
          <input id="patch-deadline" v-model="patchDeadline" placeholder="YYYY-MM-DD" />
          <div v-for="issue in fieldIssues(String(currentReview.id), 'deadline')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
          </div>

          <label class="field-label" for="patch-accept">验收标准</label>
          <textarea id="patch-accept" v-model="patchAcceptance" rows="3" />
          <p v-if="currentReview?.acceptance_origin === 'system'" class="note">验收标准是系统建议，请确认是否采用。</p>
          <div v-for="issue in fieldIssues(String(currentReview.id), 'acceptance')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
          </div>

          <p class="field-label">依据</p>
          <p v-if="currentReview?.evidence_kind === 'inference'" class="note">推断。<template v-if="currentReview.inference_reason">{{ currentReview.inference_reason }}</template></p>
          <blockquote v-if="currentReview?.evidence_quote">{{ currentReview.evidence_quote }}</blockquote>
          <p v-if="currentReview && currentReview.evidence_kind !== 'inference' && !quoteInSource(currentReview.evidence_quote)" class="note">未在当前原文中找到对应引用</p>
          <div v-for="issue in fieldIssues(String(currentReview.id), 'evidence')" :key="issue.code" class="issue">
            <p><strong>{{ issueTone(issue.severity) }}。</strong>{{ issue.message }}</p>
          </div>
          <button type="button" class="btn-ghost" @click="viewCurrentSource">在原文中查看</button>

          <div class="actions">
            <button type="submit" class="btn-primary" :disabled="loading || !reviseItemId">保存字段</button>
          </div>
          <p class="note">保存字段后，标题、日期和验收标准才会写入。批准会带上当前的负责人。</p>
        </form>
        </article>

        <details class="more-revise">
          <summary>修订全文或按说明修改</summary>
          <label class="field-label" for="revise-scope">范围</label>
          <select id="revise-scope" v-model="reviseScope">
            <option value="item">当前这项</option>
            <option value="document">全文</option>
            <option value="drop">删除</option>
          </select>
          <template v-if="reviseScope !== 'document'">
            <label class="field-label" for="revise-target">任务</label>
            <select id="revise-target" v-model="reviseTargetId">
              <option value="">请选择任务</option>
              <option v-for="task in reviewItems" :key="String(task.id)" :value="String(task.id)">
                {{ task.index }}. {{ task.title }}
              </option>
            </select>
          </template>
          <label class="field-label" for="revise-feedback">说明</label>
          <textarea id="revise-feedback" v-model="reviseFeedback" rows="3" placeholder="例如：第 2 条不是任务，删掉" />
          <button type="button" class="btn-ghost" :disabled="loading" @click="resume(false)">提交修订</button>
        </details>

        <div v-if="unsavedNotice.length" class="unsaved-dialog" role="alertdialog" aria-labelledby="unsaved-title">
          <h2 id="unsaved-title">还有未保存的修改</h2>
          <p>批准不会提交这些标题、截止日期或验收标准。可以先保存字段，或放弃这些修改后再批准。</p>
          <ul>
            <li v-for="line in unsavedNotice" :key="line">{{ line }}</li>
          </ul>
          <div class="actions">
            <button ref="unsavedBack" type="button" class="btn-primary" @click="unsavedNotice = []">返回修改</button>
            <button type="button" class="btn-ghost" @click="discardUnsavedAndApprove">放弃修改并批准</button>
          </div>
        </div>
        <form v-if="needOverride" id="override-form" class="override-form" @submit.prevent="requestApprove">
          <h2>
            填写放行理由
            <button type="button" class="btn-ghost btn-toggle" @click="showOverride = !showOverride">
              {{ showOverride ? '收起' : '展开' }}
            </button>
          </h2>
          <p class="note">本次存在阻断错误。批准会在审核记录里留下理由和自述人，请写清为什么可以放行。</p>
          <div v-show="showOverride">
            <label class="field-label" for="override-reason">放行理由（必填）</label>
            <textarea
              id="override-reason"
              v-model="overrideReason"
              rows="3"
              placeholder="例如：已与主讲人确认，第 3 条负责人由张三线下认领。"
            />
            <label class="field-label" for="operator-claim">自述操作人（选填）</label>
            <input id="operator-claim" v-model="operatorClaim" placeholder="未认证自述，仅作记录" />
            <p class="note">当前没有登录态，这个姓名不会被验证，只写进审核记录。</p>
          </div>
        </form>

        <div class="actions approve-bar">
          <button
            type="button"
            class="btn-primary btn-approve"
            :disabled="loading || (needOverride && !overrideReason.trim())"
            @click="requestApprove"
          >
            一键批准并完成
          </button>
          <p v-if="needOverride" class="note">先填写放行理由，按钮才可用。</p>
        </div>
      </section>

      <section v-else-if="phase === 'done'">
        <h1>任务已整理完成</h1>
        <p v-if="snapshot?.meeting_brief">目标：{{ snapshot.meeting_brief.goal || '未填写' }}</p>
        <p v-if="snapshot?.meeting_brief">参会人：{{ snapshot.meeting_brief.stakeholders?.join('、') || '未填写' }}</p>
        <p v-if="snapshot?.meeting_brief">日期：{{ snapshot.meeting_brief.meeting_date || '未填写' }}</p>
        <p v-if="snapshot?.idempotent" class="note">这是已保存的结果，没有重新生成。</p>
        <div v-if="extractionWarning" class="banner banner-warn" role="status">
          <p>{{ extractionWarning }}</p>
          <p v-if="extractionDetail" class="note">{{ extractionDetail }}</p>
        </div>
        <div class="actions">
          <button type="button" class="btn-primary" @click="download('md')">下载 Markdown</button>
          <button type="button" class="btn-ghost" @click="download('json')">下载 JSON</button>
          <button
            id="done-source-toggle"
            type="button"
            class="btn-ghost"
            aria-controls="source-pane"
            :aria-expanded="sourceOpen"
            @click="sourceOpen = !sourceOpen"
          >{{ sourceOpen ? '收起原文' : '查看原文' }}</button>
        </div>
        <div class="tabs" role="tablist" aria-label="结果视图" @keydown="onResultTabKey">
          <button
            id="result-tab-list"
            type="button"
            role="tab"
            aria-controls="result-panel-list"
            :aria-selected="resultView === 'list'"
            :tabindex="resultView === 'list' ? 0 : -1"
            :class="{ on: resultView === 'list' }"
            @click="resultView = 'list'"
          >任务列表</button>
          <button
            id="result-tab-people"
            type="button"
            role="tab"
            aria-controls="result-panel-people"
            :aria-selected="resultView === 'people'"
            :tabindex="resultView === 'people' ? 0 : -1"
            :class="{ on: resultView === 'people' }"
            @click="resultView = 'people'"
          >按负责人查看</button>
          <button
            id="result-tab-markdown"
            type="button"
            role="tab"
            aria-controls="result-panel-markdown"
            :aria-selected="resultView === 'markdown'"
            :tabindex="resultView === 'markdown' ? 0 : -1"
            :class="{ on: resultView === 'markdown' }"
            @click="resultView = 'markdown'"
          >Markdown</button>
        </div>
        <div v-if="resultView === 'list'" id="result-panel-list" role="tabpanel" aria-labelledby="result-tab-list" tabindex="0">
          <p v-if="!taskItems.length" class="note">这次没有整理出任务。可以修改原文后重新生成。</p>
          <article
            v-for="item in taskItems"
            :id="'task-' + item.id"
            :key="item.id"
            class="task-card"
            :class="{ selected: reviseItemId === item.id }"
          >
            <button type="button" class="text-button" @click="selectItemForRevise(item); sourceOpen = true">
              {{ item.title }}
              <span v-if="reviseItemId === item.id" class="current-flag">当前选中</span>
            </button>
            <p>负责人：{{ item.primary_owner || '待分配负责人' }}</p>
            <p v-if="item.deadline">截止日期：{{ item.deadline }}</p>
            <p v-if="labelsFor(item).length" class="flag-row">
              <span v-for="label in labelsFor(item)" :key="label" class="wait-flag">{{ label }}</span>
            </p>
            <p v-if="item.acceptance_origin === 'system'">验收标准是系统建议</p>
            <p v-if="item.evidence_kind === 'inference'">推断<span v-if="item.inference_reason">：{{ item.inference_reason }}</span></p>
            <p v-if="reviseItemId === item.id && item.evidence_quote">依据：{{ item.evidence_quote }}</p>
          </article>
          <button v-if="taskItems.length === 0" type="button" class="btn-primary" :disabled="!sourceText.trim()" @click="runWorkflow()">重新生成</button>
        </div>
        <div v-else-if="resultView === 'people'" id="result-panel-people" role="tabpanel" aria-labelledby="result-tab-people" tabindex="0">
          <TaskBoard :items="taskItems" :selected-id="reviseItemId" @select="selectItemForRevise($event); sourceOpen = true" />
        </div>
        <div v-else id="result-panel-markdown" role="tabpanel" aria-labelledby="result-tab-markdown" tabindex="0">
          <div class="tabs">
            <button type="button" class="btn-ghost" :class="{ on: mdView === 'preview' }" @click="mdView = 'preview'">预览</button>
            <button type="button" class="btn-ghost" :class="{ on: mdView === 'source' }" @click="mdView = 'source'">源码</button>
          </div>
          <div v-if="mdView === 'preview'" class="md-preview" v-html="markdownHtml" />
          <pre v-else class="md-source">{{ markdownPreview }}</pre>
        </div>
      </section>

      <section v-else-if="phase === 'failed'">
        <h1>这次没有完成</h1>
        <p>上面是这次失败的原因。改完原文后可以再生成一次。</p>
        <button
          type="button"
          class="btn-primary"
          :disabled="loading || !sourceText.trim() || sourceText.length > HARD_LIMIT"
          @click="runWorkflow()"
        >
          重新生成
        </button>
      </section>

      <ProgressLog v-if="showLog" :entries="progressLog" :tools="toolLog" />
    </main>
  </div>
</template>

<style scoped>
.product { font-size: 0.875rem; color: var(--muted); }
h1 { font-size: 1.75rem; line-height: 1.3; font-weight: 650; margin: 0.35rem 0 0.5rem; }
h2 { font-size: 1.125rem; line-height: 1.4; font-weight: 650; }
.lead p, .note { color: var(--muted); }
.lead-copy { font-size: 1.125rem; margin-bottom: 0.75rem; }
.stages { display: flex; flex-wrap: wrap; gap: 0.75rem 1.25rem; list-style: none; margin-top: 0.75rem; }
.stages li { color: var(--muted); padding-bottom: 0.2rem; }
.stages .current { color: var(--primary); font-weight: 650; border-bottom: 2px solid var(--primary); }
.sample-row, .actions, .chip-row, .tabs, .row, .source-tools { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.actions { margin-top: 0.75rem; }
.field-label { display: block; margin: 0.75rem 0 0.25rem; color: var(--muted); font-size: 0.875rem; }
.check { display: flex; align-items: center; gap: 0.4rem; min-height: 44px; }
.check input { width: auto; min-height: 0; }
.banner {
  background: var(--danger-bg);
  color: var(--danger);
  border: 1px solid var(--danger);
  border-radius: var(--radius);
  padding: 0.85rem 1rem;
}
.banner .btn-ghost { margin-top: 0.5rem; color: var(--ink); }
/* 抽取不完整只是风险提示，用琥珀而不是危险色，避免和真正的失败混在一起。 */
.banner-warn {
  margin: 0.75rem 0;
  background: var(--amber-bg);
  color: var(--amber);
  border-color: var(--amber);
}
.issue-blocking { border-left: 3px solid var(--danger); padding-left: 0.7rem; }
.issue-blocking > p { margin-bottom: 0.35rem; }
.override-form { margin: 1rem 0; padding: 0.85rem 1rem; border: 1px solid var(--line); border-radius: var(--radius); }
.override-form h2 { display: flex; align-items: center; gap: 0.5rem; font-size: 1.05rem; }
.override-form .btn-toggle { margin-left: auto; }
.override-form textarea { min-height: 5rem; }
.assign-row { padding: 0.85rem 0; border-bottom: 1px solid var(--line); }
.assign-row.selected, .task-card.selected { background: var(--selected); }
.task-card { padding: 0.85rem 0.35rem; border-bottom: 1px solid var(--line); scroll-margin-top: 1rem; }
.flag-row { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.unsaved-dialog {
  margin: 0.75rem 0;
  padding: 0.85rem 1rem;
  background: var(--amber-bg);
  border-left: 3px solid var(--amber);
  color: var(--ink);
}
.unsaved-dialog h2 { font-size: 1.05rem; }
.unsaved-dialog ul { margin: 0.5rem 0 0 1.1rem; }
.task-card h2, .assign-locate h2 { font-size: 1.05rem; }
.current-flag { color: var(--primary); font-size: 0.875rem; font-weight: 650; margin-left: 0.35rem; }
.wait-flag {
  margin-left: 0.35rem;
  background: var(--amber-bg);
  color: var(--amber);
  border-radius: var(--radius-sm);
  padding: 0.05rem 0.35rem;
  font-size: 0.875rem;
}
.index { margin-right: 0.25rem; }
.text-button { width: auto; flex: 1; text-align: left; background: transparent; border: none; font-weight: 650; }
.locate-link {
  background: transparent;
  border: none;
  padding: 0;
  min-height: 0;
  color: var(--primary);
  text-decoration: underline;
  font: inherit;
}
.locate-link:hover { color: var(--primary-hover); }
.row { justify-content: space-between; align-items: flex-start; }
.editor { margin-top: 1rem; padding-top: 0.5rem; border-top: 1px solid var(--line); }
.editor textarea { min-height: 6rem; }
.issue {
  margin: 0.35rem 0 0.5rem;
  padding: 0.55rem 0.7rem;
  background: var(--amber-bg);
  border-left: 3px solid var(--amber);
  color: var(--amber);
}
.issue p { overflow-wrap: anywhere; }
.issue summary { color: var(--ink); }
blockquote {
  margin: 0.35rem 0;
  padding-left: 0.75rem;
  border-left: 3px solid var(--mark);
  overflow-wrap: anywhere;
  line-height: var(--source-line);
}
.locate-hint { color: var(--amber); }
.chip, .suggest-chip {
  background: var(--surface);
  color: var(--ink);
  border: 1px solid var(--line);
}
.chip.on, .suggest-chip.on { border-color: var(--primary); background: var(--selected); color: var(--primary); }
.suggest-chip { display: flex; flex-direction: column; align-items: flex-start; }
.suggest-band { color: var(--muted); font-size: 0.875rem; }
.approve-bar {
  position: sticky;
  bottom: 0;
  z-index: 2;
  padding: 0.75rem 0;
  background: var(--bg);
  border-top: 1px solid var(--line);
}
@media (max-width: 960px) {
  .approve-bar { position: static; }
}
.more-revise { margin-top: 1rem; }
.tabs { margin: 1rem 0 0.75rem; }
.tabs .on { border-color: var(--primary); color: var(--primary); }
p { overflow-wrap: anywhere; }
</style>
