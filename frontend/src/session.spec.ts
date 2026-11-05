import { beforeEach, describe, expect, it } from 'vitest'
import { acceptResponse, confirmHeadline, confirmLabels, confirmOwner, confirmRank, cycleIndex, fieldChanges, isSettledStatus, issueField, moveTabIndex, needsSnapshotRefresh, overlayReviewItem, ownerOriginals, rememberedThread, rememberThread, resolveDraft, reviewOrder, shouldFollowPlanning, THREAD_STORAGE_KEY, viewPhase } from './session'

describe('session recovery', () => {
  beforeEach(() => sessionStorage.clear())

  it('keeps the thread id for this tab', () => {
    rememberThread('thread-1')
    expect(sessionStorage.getItem(THREAD_STORAGE_KEY)).toBe('thread-1')
    expect(rememberedThread()).toBe('thread-1')
  })

  it('treats review, completion, and failure as settled', () => {
    expect(isSettledStatus('awaiting_review')).toBe(true)
    expect(isSettledStatus('completed')).toBe(true)
    expect(isSettledStatus('failed')).toBe(true)
    expect(isSettledStatus('planning')).toBe(false)
  })

  it('drops a response from an older run', () => {
    expect(acceptResponse(2, 2)).toBe(true)
    expect(acceptResponse(1, 2)).toBe(false)
  })

  it('keeps polling only while the server is still planning', () => {
    expect(shouldFollowPlanning('planning')).toBe(true)
    expect(shouldFollowPlanning('awaiting_review')).toBe(false)
    expect(shouldFollowPlanning('completed')).toBe(false)
  })

  it('refreshes after a failed resume even if the last panel was already review', () => {
    expect(needsSnapshotRefresh(false)).toBe(true)
    expect(isSettledStatus('awaiting_review')).toBe(true)
    expect(needsSnapshotRefresh(true)).toBe(false)
  })

  it('confirms an owner only after a real edit or an explicit check', () => {
    const originals = ownerOriginals([{ id: 'a', primary_owner: '张三' }])
    expect(confirmOwner(false, '张三', originals.a)).toBe(false)
    expect(confirmOwner(false, '李四', originals.a)).toBe(true)
    expect(confirmOwner(true, '张三', originals.a)).toBe(true)
    expect(confirmOwner(false, '张三', ownerOriginals(undefined).a || '')).toBe(true)
  })

  it('follows the server status and the current interrupt', () => {
    expect(viewPhase({ restoring: true, loading: false })).toBe('restore')
    expect(viewPhase({ restoring: false, loading: true, status: 'completed' })).toBe('running')
    expect(viewPhase({ restoring: false, loading: false, interruptType: 'assign_owners' })).toBe('assign')
    expect(viewPhase({ restoring: false, loading: false, interruptType: 'review', status: 'completed' })).toBe('review')
    expect(viewPhase({ restoring: false, loading: false, status: 'awaiting_review' })).toBe('restore')
    expect(viewPhase({ restoring: false, loading: false, status: 'failed' })).toBe('failed')
    expect(viewPhase({ restoring: false, loading: false })).toBe('intake')
  })

  it('puts tasks that need confirmation first and keeps the rest in order', () => {
    const items = [
      { id: 'a', primary_owner: '张三' },
      { id: 'b', primary_owner: '' },
      { id: 'c', primary_owner: '李四' },
      { id: 'd', primary_owner: '王五' },
    ]
    const ordered = reviewOrder(
      items,
      [
        { item_id: 'c', severity: 'error' },
        { item_id: 'd', severity: 'advisory' },
      ],
      (item) => item.primary_owner,
    )
    expect(ordered.map((item) => item.id)).toEqual(['c', 'b', 'd', 'a'])
    expect(issueField('deadline')).toBe('deadline')
    expect(issueField('evidence_quote')).toBe('evidence')
    expect(issueField(undefined)).toBe('general')
  })

  it('uses one rule for the count, the card label, and the sort', () => {
    const quoteError = [{ severity: 'error' }]
    const dateOnly = [{ severity: 'advisory' }]
    expect(confirmRank(quoteError, '张三')).toBe(0)
    expect(confirmLabels(quoteError, '张三')).toEqual(['需修改'])
    expect(confirmRank([], '')).toBe(1)
    expect(confirmLabels([], '')).toEqual(['待分配负责人'])
    expect(confirmRank(dateOnly, '张三')).toBe(2)
    expect(confirmLabels(dateOnly, '张三')).toEqual(['建议补充'])
    expect(confirmLabels(quoteError, '')).toEqual(['需修改', '待分配负责人'])
    expect(confirmHeadline({ total: 4, fix: 2, owner: 1, advise: 1, overlap: 1 })).toBe(
      '共 4 项。需修改 2 项，待分配负责人 1 项，建议补充 1 项。同一任务可同时计入需修改和待分配负责人。',
    )
    expect(confirmHeadline({ total: 3, fix: 0, owner: 0, advise: 0, overlap: 0 })).toBe(
      '共 3 项。没有待确认的问题。',
    )
  })

  it('keeps a field draft and names unsaved title, date, and acceptance changes', () => {
    const saved = { title: '原标题', deadline: '', acceptance: '完成' }
    expect(resolveDraft(undefined, saved)).toEqual(saved)
    const draft = { title: '新标题', deadline: '2026-12-01', acceptance: '完成' }
    expect(resolveDraft(draft, saved)).toEqual(draft)
    expect(fieldChanges(draft, saved)).toEqual(['标题', '截止日期'])
    expect(fieldChanges(saved, saved)).toEqual([])
  })

  it('fills review rows from the task draft without replacing values already there', () => {
    const row = overlayReviewItem(
      { id: 'a', title: '已改标题', evidence_quote: '' } as Record<string, unknown>,
      {
        id: 'a',
        title: '原标题',
        acceptance_origin: 'system',
        evidence_kind: 'inference',
        inference_reason: '原文没有写负责人',
        evidence_quote: '不应覆盖空引用',
        deadline: '2026-04-11',
      },
    )
    expect(row.title).toBe('已改标题')
    expect(row.acceptance_origin).toBe('system')
    expect(row.evidence_kind).toBe('inference')
    expect(row.inference_reason).toBe('原文没有写负责人')
    expect(row.evidence_quote).toBe('不应覆盖空引用')
    expect(row.deadline).toBe('2026-04-11')
    expect(overlayReviewItem({ id: 'b', acceptance_origin: 'human' }, { acceptance_origin: 'system' }).acceptance_origin).toBe('human')
  })

  it('moves result tabs with arrows and cycles focus', () => {
    expect(moveTabIndex(0, 3, 'ArrowRight')).toBe(1)
    expect(moveTabIndex(0, 3, 'ArrowLeft')).toBe(2)
    expect(moveTabIndex(1, 3, 'Home')).toBe(0)
    expect(moveTabIndex(1, 3, 'End')).toBe(2)
    expect(moveTabIndex(0, 3, 'Enter')).toBeNull()
    expect(cycleIndex(2, 3, false)).toBe(0)
    expect(cycleIndex(0, 3, true)).toBe(2)
  })
})
