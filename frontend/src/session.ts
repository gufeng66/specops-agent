/** 同一标签页的会话恢复。关闭标签后不保留。 */

export const THREAD_STORAGE_KEY = 'specops.threadId'

export function rememberedThread(): string | null {
  return sessionStorage.getItem(THREAD_STORAGE_KEY)
}

export function rememberThread(threadId: string) {
  sessionStorage.setItem(THREAD_STORAGE_KEY, threadId)
}

export function isSettledStatus(status: string | undefined): boolean {
  return status === 'awaiting_review' || status === 'completed' || status === 'failed'
}

export function acceptResponse(seq: number, current: number): boolean {
  return seq === current
}

/**
 * 快照里与「这次响应是否还属于当前页面」相关的字段。
 * 服务端会把中断相关字段写成 null（例如已结束的执行），所以这里允许 null：
 * 下面几个判断都先做 String(... || '') 归一，null 与缺失等价。
 */
export interface SnapshotRef {
  thread_id?: string | null
  run_id?: string | null
  status?: string | null
  revision?: number | null
  interrupt_id?: string | null
  interrupt_payload?: Record<string, unknown> | null
}

export interface SnapshotMeta {
  /** 发起这次请求时的代次。 */
  seq: number
  /** 当前代次。两者不等说明用户已经开了新的一次执行。 */
  currentSeq: number
  /** 当前页面对应的线程；null 表示还没确定，不做约束。 */
  threadId: string | null
  /** 当前页面已认下的 run_id；null 表示还没确定，不做约束。 */
  runId: string | null
}

export interface SnapshotVerdict {
  ok: boolean
  reason?: string
}

/**
 * 判断一份服务端快照能不能覆盖页面。四道闸门依次是：
 * 代次、线程、run、逻辑版本。任何一道不过都不写，避免迟到的旧响应把新状态盖回去。
 * threadId / runId 为 null 时跳过对应校验：页面还没认下标识，没有可保护的状态。
 */
export function acceptSnapshot(
  prev: SnapshotRef | null | undefined,
  next: SnapshotRef,
  meta: SnapshotMeta,
): SnapshotVerdict {
  if (!acceptResponse(meta.seq, meta.currentSeq)) return { ok: false, reason: 'stale-seq' }
  if (meta.threadId && String(next.thread_id || '') !== meta.threadId) {
    return { ok: false, reason: 'thread-mismatch' }
  }
  if (meta.runId && String(next.run_id || '') !== meta.runId) {
    return { ok: false, reason: 'run-mismatch' }
  }
  const incoming = Number(next.revision ?? 0)
  const current = Number(prev?.revision ?? 0)
  if (!(incoming >= current)) return { ok: false, reason: 'stale-revision' }
  return { ok: true }
}

/**
 * 当前应该交给界面的 interrupt。
 * 只有服务端明确停在待确认、并且这份 payload 属于当前中断时才返回，否则一律 null：
 * completed / failed 之后残留的旧 interrupt 不能继续驱动页面。
 * 两侧都缺 interrupt_id 时无法比对，按「没有可校验的标识」放行，避免整块审核面板消失。
 */
export function nextInterrupt(snap: SnapshotRef | null | undefined): Record<string, unknown> | null {
  if (!snap || snap.status !== 'awaiting_review') return null
  const payload = snap.interrupt_payload
  if (!payload || typeof payload !== 'object') return null
  const expected = String(snap.interrupt_id || '')
  const got = String((payload as Record<string, unknown>).interrupt_id || '')
  if (expected && got && expected !== got) return null
  return payload
}

export function shouldFollowPlanning(status: string | undefined): boolean {
  return status === 'planning'
}

/** 这次请求没有最终结果时必须再查快照。不能用上一次的状态跳过。 */
export function needsSnapshotRefresh(gotResult: boolean): boolean {
  return !gotResult
}

export function ownerOriginals(
  items: Array<{ id?: string; primary_owner?: string }> | undefined,
): Record<string, string> {
  const map: Record<string, string> = {}
  for (const item of items || []) {
    const id = String(item.id || '')
    if (id) map[id] = String(item.primary_owner || '')
  }
  return map
}

export function confirmOwner(checked: boolean, draft: string, original: string): boolean {
  return checked || draft !== original
}

export type ViewPhase =
  | 'restore'
  | 'intake'
  | 'running'
  | 'clarify'
  | 'assign'
  | 'review'
  | 'done'
  | 'failed'

/**
 * 页面只认服务端状态和当前 interrupt，不根据本地猜测推进阶段。
 * 顺序是有意的：先恢复态，再看中断，然后看是否在跑，最后才是终态。
 * 中断类型只在服务端确实停在 awaiting_review 时才算数——completed / failed
 * 之后残留的旧 interrupt（比如上个页面没清掉的 review）不允许把界面推回审核态。
 */
export function viewPhase(input: {
  restoring: boolean
  interruptType?: string
  loading: boolean
  status?: string
}): ViewPhase {
  if (input.restoring) return 'restore'
  if (input.status === 'awaiting_review') {
    if (input.interruptType === 'clarify') return 'clarify'
    if (input.interruptType === 'assign_owners') return 'assign'
    if (input.interruptType === 'review') return 'review'
  }
  if (input.loading || input.status === 'planning') return 'running'
  if (input.status === 'completed') return 'done'
  if (input.status === 'failed') return 'failed'
  if (input.status === 'awaiting_review') return 'restore'
  return 'intake'
}

export type IssueSlot = 'title' | 'owner' | 'secondary' | 'deadline' | 'acceptance' | 'evidence' | 'general'

export function issueField(field?: string | null): IssueSlot {
  if (field === 'title') return 'title'
  if (field === 'primary_owner') return 'owner'
  if (field === 'secondary_owners') return 'secondary'
  if (field === 'deadline') return 'deadline'
  if (field === 'acceptance_criteria') return 'acceptance'
  if (field === 'evidence_quote' || field === 'inference_reason') return 'evidence'
  return 'general'
}

/** 0 需修改，1 待分配负责人，2 建议补充，3 无问题。排序和卡片标识都用这个。 */
export function confirmRank(issues: Array<{ severity: string }>, primary: string): number {
  if (issues.some((issue) => issue.severity === 'error')) return 0
  if (!primary.trim()) return 1
  if (issues.length) return 2
  return 3
}

export function confirmLabels(issues: Array<{ severity: string }>, primary: string): string[] {
  const labels: string[] = []
  if (issues.some((issue) => issue.severity === 'error')) labels.push('需修改')
  if (!primary.trim()) labels.push('待分配负责人')
  if (issues.some((issue) => issue.severity !== 'error')) labels.push('建议补充')
  return labels
}

export function confirmHeadline(counts: {
  total: number
  fix: number
  owner: number
  advise: number
  overlap: number
}): string {
  const bits: string[] = []
  if (counts.fix) bits.push(`需修改 ${counts.fix} 项`)
  if (counts.owner) bits.push(`待分配负责人 ${counts.owner} 项`)
  if (counts.advise) bits.push(`建议补充 ${counts.advise} 项`)
  if (!bits.length) return `共 ${counts.total} 项。没有待确认的问题。`
  let text = `共 ${counts.total} 项。${bits.join('，')}。`
  if (counts.overlap) text += '同一任务可同时计入需修改和待分配负责人。'
  return text
}

export function fieldChanges(
  current: { title: string; deadline: string; acceptance: string },
  saved: { title: string; deadline: string; acceptance: string },
): string[] {
  const changed: string[] = []
  if (current.title !== saved.title) changed.push('标题')
  if (current.deadline !== saved.deadline) changed.push('截止日期')
  if (current.acceptance !== saved.acceptance) changed.push('验收标准')
  return changed
}

/** 切到某项时用已有草稿，没有才用服务端原值。 */
export function resolveDraft<T>(stored: T | undefined, saved: T): T {
  return stored ?? saved
}

const REVIEW_FILL = [
  'deadline',
  'acceptance_criteria',
  'acceptance_origin',
  'evidence_kind',
  'inference_reason',
  'evidence_quote',
] as const

/** 审核列表的 interrupt 行没有来源字段时，用任务草案补上。已有值不动。 */
export function overlayReviewItem<T extends Record<string, unknown>>(
  row: T,
  full?: Record<string, unknown> | null,
): T {
  if (!full) return row
  const extra: Record<string, unknown> = {}
  for (const key of REVIEW_FILL) {
    if (row[key] == null || row[key] === '') extra[key] = full[key] ?? ''
  }
  return { ...row, ...extra }
}

export function moveTabIndex(current: number, count: number, key: string): number | null {
  if (count <= 0) return null
  if (key === 'ArrowRight') return (current + 1) % count
  if (key === 'ArrowLeft') return (current - 1 + count) % count
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  return null
}

export function cycleIndex(index: number, count: number, backward: boolean): number {
  if (count <= 0) return 0
  const current = index < 0 ? 0 : index
  return backward ? (current - 1 + count) % count : (current + 1) % count
}

/** 待确认的任务排在前面，其余保持原顺序。 */
export function reviewOrder<T extends { id?: unknown }>(
  items: T[],
  issues: Array<{ item_id?: string | null; severity: string }>,
  primaryOf: (item: T) => string,
): T[] {
  const rank = (item: T) => {
    const id = String(item.id || '')
    const mine = issues.filter((issue) => (issue.item_id || '') === id)
    return confirmRank(mine, primaryOf(item))
  }
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => rank(a.item) - rank(b.item) || a.index - b.index)
    .map((row) => row.item)
}
