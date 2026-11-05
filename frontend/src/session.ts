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

/** 页面只认服务端状态和当前 interrupt，不根据本地猜测推进阶段。 */
export function viewPhase(input: {
  restoring: boolean
  interruptType?: string
  loading: boolean
  status?: string
}): ViewPhase {
  if (input.restoring) return 'restore'
  if (input.interruptType === 'clarify') return 'clarify'
  if (input.interruptType === 'assign_owners') return 'assign'
  if (input.interruptType === 'review') return 'review'
  if (input.loading || input.status === 'planning') return 'running'
  if (input.status === 'completed') return 'done'
  if (input.status === 'failed') return 'failed'
  if (input.status === 'awaiting_review') return 'restore'
  return 'intake'
}

export type IssueSlot = 'title' | 'owner' | 'secondary' | 'deadline' | 'acceptance' | 'evidence' | 'general'

export function issueField(field?: string): IssueSlot {
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
  issues: Array<{ item_id?: string; severity: string }>,
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
