import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'

const SOURCE = [
  '会议主题：需求评审待办确认会。',
  '这个跟进交给她负责，本周内完成。',
  '需求变更记录由她负责整理，截止时间为2026年4月15日18:00。',
].join('\n')

function assignItem(id: string, title: string, quote: string) {
  return {
    id,
    title,
    description: quote,
    primary_owner: '',
    secondary_owners: [],
    evidence_quote: quote,
    unresolved_ref: '她',
    owner_suggestions: [],
    issues: [],
  }
}

function assignPayload() {
  return {
    type: 'assign_owners',
    interrupt_id: 'int-assign',
    message: '请为无法确认负责人的任务分配主责/次责',
    stakeholders: ['张伟', '李娜'],
    items: [
      assignItem('a1', '跟进事项', '这个跟进交给她负责，本周内完成。'),
      assignItem('a2', '需求变更记录整理', '需求变更记录由她负责整理，截止时间为2026年4月15日18:00。'),
    ],
    attempt: 1,
    max_attempts: 3,
  }
}

function snap() {
  return {
    thread_id: 'thread-1',
    run_id: 'run-1',
    status: 'awaiting_assign',
    source_text: SOURCE,
    meeting_brief: { goal: '目标', stakeholders: ['张伟', '李娜'], meeting_date: '' },
    task_draft: { items: [] },
    validation: { issues: [] },
    markdown_output: '',
  }
}

function sse(events: Array<[string, unknown]>) {
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function installFetch() {
  vi.stubGlobal('fetch', async (input: RequestInfo) => {
    const url = String(input)
    if (url.includes('/chat/stream')) {
      return sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap()],
        ['interrupt', assignPayload()],
        ['done', {}],
      ])
    }
    if (url.includes('/api/v1/threads/')) {
      return Response.json({ ...snap(), interrupt_payload: assignPayload() })
    }
    return new Response('missing', { status: 404 })
  })
}

async function openAssign(): Promise<VueWrapper> {
  const wrapper = mount(App, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('#meeting-source').setValue(SOURCE)
  await wrapper.findAll('button').find((btn) => btn.text() === '生成任务')!.trigger('click')
  await flushPromises()
  return wrapper
}

function markedText(wrapper: VueWrapper): string {
  return wrapper.find('.source-read mark').exists() ? wrapper.get('.source-read mark').text() : ''
}

describe('assign 阶段定位原文', () => {
  beforeEach(() => {
    sessionStorage.clear()
    Element.prototype.scrollIntoView = () => {}
    vi.spyOn(window, 'open').mockImplementation(() => null)
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('点「点击这里定位原文」能标出该任务的依据', async () => {
    installFetch()
    const wrapper = await openAssign()
    expect(wrapper.find('.hitl-assign').exists()).toBe(true)
    expect(markedText(wrapper)).toBe('')

    await wrapper.get('.hitl-assign .locate-link').trigger('click')
    await flushPromises()

    expect(markedText(wrapper)).toBe('这个跟进交给她负责，本周内完成。')
    wrapper.unmount()
  })

  it('点标题也能标出依据，且两条任务各自对应自己的句子', async () => {
    installFetch()
    const wrapper = await openAssign()

    await wrapper.findAll('button').find((btn) => btn.text().includes('跟进事项'))!.trigger('click')
    await flushPromises()
    expect(markedText(wrapper)).toBe('这个跟进交给她负责，本周内完成。')

    await wrapper.findAll('button').find((btn) => btn.text().includes('需求变更记录整理'))!.trigger('click')
    await flushPromises()
    expect(markedText(wrapper)).toBe('需求变更记录由她负责整理，截止时间为2026年4月15日18:00。')
    wrapper.unmount()
  })

  it('找不到引用时给出提示而不是静默失败', async () => {
    installFetch()
    const wrapper = await openAssign()

    const input = wrapper.get('#meeting-source')
    await input.setValue('会议主题：需求评审待办确认会。')
    await wrapper.findAll('button').find((btn) => btn.text().includes('跟进事项'))!.trigger('click')
    await flushPromises()

    expect(markedText(wrapper)).toBe('')
    expect(wrapper.text()).toContain('未在当前原文中找到对应引用')
    wrapper.unmount()
  })
})
