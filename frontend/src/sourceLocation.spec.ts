import { describe, expect, it } from 'vitest'
import { findQuoteRange, isHitlInteractiveTarget, quoteCandidates } from './sourceLocation'

const item = {
  id: 'task-1',
  title: '整理需求变更',
  description: '需求变更记录由张伟负责整理。',
  primary_owner: '张伟',
  secondary_owners: [],
  evidence_quote: '需求变更记录由张伟负责整理。',
}

describe('source location helpers', () => {
  it('uses evidence first and removes duplicate candidates', () => {
    expect(quoteCandidates(item)).toEqual([
      '需求变更记录由张伟负责整理。',
      '整理需求变更',
    ])
  })

  it('returns the first matching quote range', () => {
    expect(findQuoteRange('会议记录\n需求变更记录由张伟负责整理。', item)).toEqual({
      start: 5,
      end: 19,
    })
    expect(findQuoteRange('没有对应内容', item)).toBeNull()
  })

  it('recognizes interactive HITL controls', () => {
    const button = document.createElement('button')
    const controls = document.createElement('div')
    controls.className = 'assign-controls'
    controls.append(button)
    document.body.append(controls)

    expect(isHitlInteractiveTarget(button)).toBe(true)
    expect(isHitlInteractiveTarget(controls)).toBe(true)
    expect(isHitlInteractiveTarget(document.createElement('div'))).toBe(false)
    expect(isHitlInteractiveTarget(null)).toBe(false)

    controls.remove()
  })
})
