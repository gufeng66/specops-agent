import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'

function items() {
  return [
    {
      id: 't0',
      index: 1,
      title: '任务A',
      primary_owner: 'Alice',
      secondary_owners: [],
      evidence_quote: '',
      acceptance_criteria: '验收A',
      deadline: '',
    },
  ]
}

function block(code: string, message: string) {
  return { code, message, severity: 'error', item_id: 't0', field: 'primary_owner' }
}

function review(over: Record<string, unknown> = {}) {
  return { type: 'review', interrupt_id: 'int-1', message: '核对任务', items: items(), ...over }
}

function snap(over: { interrupt?: Record<string, unknown>; snapshot?: Record<string, unknown> } = {}) {
  const payload = review(over.interrupt ?? {})
  return {
    thread_id: 'thread-1',
    run_id: 'run-1',
    status: 'awaiting_review',
    revision: 1,
    source_text: '会议原文',
    meeting_brief: { goal: '目标', stakeholders: ['Alice'], meeting_date: '' },
    task_draft: { items: items() },
    validation: { issues: [] },
    markdown_output: '',
    interrupt_id: 'int-1',
    interrupt_payload: payload,
    extraction: { chunks_total: 3, chunks_ok: 3, chunks_empty: 0, chunks_failed: 0, partial: false, degraded: false },
    ...(over.snapshot ?? {}),
  }
}

function sse(events: Array<[string, unknown]>) {
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const resumes: Array<Record<string, unknown>> = []

function installFetch(snapshot: Record<string, unknown>) {
  vi.stubGlobal('fetch', async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/chat/stream')) {
      return sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snapshot],
        ['done', {}],
      ])
    }
    if (url.includes('/resume')) {
      resumes.push(JSON.parse(String(init?.body || '{}')))
      return sse([['done', {}]])
    }
    if (url.includes('/api/v1/threads/')) return Response.json(snapshot)
    return new Response('missing', { status: 404 })
  })
}

async function openReview(snapshot: Record<string, unknown>): Promise<VueWrapper> {
  installFetch(snapshot)
  const wrapper = mount(App, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('#meeting-source').setValue('会议原文')
  await wrapper.findAll('button').find((btn) => btn.text() === '生成任务')!.trigger('click')
  await flushPromises()
  return wrapper
}

function approveButton(wrapper: VueWrapper) {
  return wrapper.findAll('button').find((btn) => btn.text() === '一键批准并完成')!
}

describe('review override', () => {
  beforeEach(() => {
    resumes.length = 0
    sessionStorage.clear()
    Element.prototype.scrollIntoView = () => {}
    vi.spyOn(window, 'open').mockImplementation(() => null)
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('keeps plain approve available when nothing blocks', async () => {
    const wrapper = await openReview(snap({ interrupt: { blocking_errors: [] } }))

    expect(wrapper.find('#override-reason').exists()).toBe(false)
    expect(approveButton(wrapper).attributes('disabled')).toBeUndefined()

    await approveButton(wrapper).trigger('click')
    await flushPromises()
    expect(resumes.at(-1)?.approve).toBe(true)
    // 没有阻断错误就不该携带放行理由。
    expect(resumes.at(-1)).not.toHaveProperty('override_reason')
    expect(resumes.at(-1)).not.toHaveProperty('operator_claim')
    wrapper.unmount()
  })

  it('blocks approve until a reason is typed when errors exist', async () => {
    const wrapper = await openReview(snap({
      interrupt: { blocking_errors: [block('OWNER_MISSING', '第 1 条任务没有主责')] },
    }))

    expect(wrapper.text()).toContain('第 1 条任务没有主责')
    expect(wrapper.text()).toContain('OWNER_MISSING')
    // 按钮先禁用，理由填好才放开。
    expect(approveButton(wrapper).attributes('disabled')).toBeDefined()

    const reason = wrapper.get('#override-reason')
    await reason.setValue('   ')
    expect(approveButton(wrapper).attributes('disabled')).toBeDefined()

    await reason.setValue('已与主讲人确认，负责人线下认领')
    expect(approveButton(wrapper).attributes('disabled')).toBeUndefined()

    await wrapper.get('#operator-claim').setValue('张伟')
    await approveButton(wrapper).trigger('click')
    await flushPromises()

    expect(resumes.at(-1)?.approve).toBe(true)
    expect(resumes.at(-1)?.override_reason).toBe('已与主讲人确认，负责人线下认领')
    expect(resumes.at(-1)?.operator_claim).toBe('张伟')
    wrapper.unmount()
  })

  it('never sends the override fields with a non-approve submit', async () => {
    const wrapper = await openReview(snap({
      interrupt: { blocking_errors: [block('OWNER_MISSING', '第 1 条任务没有主责')] },
    }))
    await wrapper.get('#override-reason').setValue('先写好的理由')
    await wrapper.findAll('button').find((btn) => btn.text() === '提交修订')!.trigger('click')
    await flushPromises()

    expect(resumes.at(-1)?.approve).toBe(false)
    expect(resumes.at(-1)).not.toHaveProperty('override_reason')
    wrapper.unmount()
  })

  it('warns on the review panel when extraction was incomplete', async () => {
    const wrapper = await openReview(snap({
      interrupt: {
        blocking_errors: [],
        extraction: { chunks_total: 4, chunks_ok: 2, chunks_empty: 0, chunks_failed: 2, partial: true, degraded: false },
      },
    }))

    expect(wrapper.text()).toContain('本次有 2 个分块抽取失败，结果可能不完整，请人工确认。')
    expect(wrapper.text()).toContain('审核任务清单')
    wrapper.unmount()
  })

  it('says the result is rules-only when extraction degraded without failures', async () => {
    const wrapper = await openReview(snap({
      interrupt: {
        blocking_errors: [],
        extraction: { chunks_total: 2, chunks_ok: 0, chunks_empty: 0, chunks_failed: 0, partial: false, degraded: true },
      },
    }))

    expect(wrapper.text()).toContain('仅规则结果')
    wrapper.unmount()
  })

  it('keeps the unsaved draft dialog working with the override flow', async () => {
    const wrapper = await openReview(snap({
      interrupt: { blocking_errors: [block('OWNER_MISSING', '第 1 条任务没有主责')] },
    }))
    await wrapper.get('#override-reason').setValue('理由写好了')
    await wrapper.findAll('button').find((btn) => btn.text().includes('任务A'))!.trigger('click')
    await wrapper.get('#patch-title').setValue('改过的A')

    await approveButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('还有未保存的修改')
    expect(resumes.some((body) => body.approve === true)).toBe(false)

    await wrapper.findAll('button').find((btn) => btn.text() === '放弃修改并批准')!.trigger('click')
    await flushPromises()
    expect(resumes.at(-1)?.approve).toBe(true)
    expect(resumes.at(-1)?.override_reason).toBe('理由写好了')
    wrapper.unmount()
  })
})
