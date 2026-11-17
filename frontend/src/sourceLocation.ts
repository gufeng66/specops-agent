import type { BoardItem } from './TaskBoard.vue'

/**
 * 生成原文定位候选。证据引用优先，描述和标题只作为无法直接引用时的回退。
 */
export function quoteCandidates(item: BoardItem): string[] {
  const out: string[] = []
  for (const raw of [item.evidence_quote, item.description, item.title]) {
    const text = (raw || '').trim()
    if (text && !out.includes(text)) out.push(text)
  }
  return out
}

/** 返回原文中第一条匹配候选的字符范围。 */
export function findQuoteRange(
  haystack: string,
  item: BoardItem,
): { start: number; end: number } | null {
  for (const quote of quoteCandidates(item)) {
    const start = haystack.indexOf(quote)
    if (start >= 0) return { start, end: start + quote.length }
  }
  return null
}

/** 分配面板上的交互控件不能触发整行的原文定位。 */
export function isHitlInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  return Boolean(
    target.closest('select, option, button, input, textarea, label, .assign-controls'),
  )
}
