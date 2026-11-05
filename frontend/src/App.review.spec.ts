import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'

const markdown = '# 会议纪要\n\n1. **任务A** · 2026-11-04\n'

function items(titles: { t0?: string; t2?: string } = {}) {
  return [
    {
      id: 't2',
      index: 1,
      title: titles.t2 || '任务B',
      primary_owner: 'Alice',
      secondary_owners: [],
      evidence_quote: '',
      acceptance_criteria: '验收B',
      deadline: '',
    },
    {
      id: 't0',
      index: 2,
      title: titles.t0 || '任务A',
      primary_owner: 'Alice',
      secondary_owners: [],
      evidence_quote: '',
      acceptance_criteria: '验收A',
      deadline: '',
    },
  ]
}

function snap(rows = items(), status = 'awaiting_review') {
  return {
    thread_id: 'thread-1',
    run_id: 'run-1',
    status,
    source_text: '会议原文',
    meeting_brief: { goal: '目标', stakeholders: ['Alice'], meeting_date: '' },
    task_draft: { items: rows },
    validation: { issues: [] },
    markdown_output: status === 'completed' ? markdown : '',
  }
}

function review(rows = items()) {
  return { type: 'review', interrupt_id: 'int-1', message: '核对任务', items: rows }
}

function sse(events: Array<[string, unknown]>) {
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const resumes: Array<Record<string, unknown>> = []

function installFetch(resume?: () => Response) {
  vi.stubGlobal('fetch', async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/chat/stream')) {
      return sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap()],
        ['interrupt', review()],
        ['done', {}],
      ])
    }
    if (url.includes('/resume')) {
      resumes.push(JSON.parse(String(init?.body || '{}')))
      return resume ? resume() : sse([['done', {}]])
    }
    if (url.includes('/export?format=json')) {
      return new Response(JSON.stringify({ title: '任务A', deadline: '2026-11-04' }))
    }
    if (url.includes('/export?format=md')) return new Response(markdown)
    if (url.includes('/api/v1/threads/')) {
      return Response.json({ ...snap(), interrupt_payload: review() })
    }
    return new Response('missing', { status: 404 })
  })
}

async function openReview() {
  const wrapper = mount(App, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('#meeting-source').setValue('会议原文')
  await wrapper.findAll('button').find((btn) => btn.text() === '生成任务')!.trigger('click')
  await flushPromises()
  return wrapper
}

function clickTask(wrapper: VueWrapper, title: string) {
  return wrapper.findAll('button').find((btn) => btn.text().includes(title))!.trigger('click')
}

describe('review drafts', () => {
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

  it('keeps the field editor when the revision target changes', async () => {
    installFetch(() => sse([['interrupt', review()], ['result', snap()], ['done', {}]]))
    const wrapper = await openReview()
    await clickTask(wrapper, '任务A')
    await wrapper.get('#patch-title').setValue('改过的A')
    await wrapper.get('#patch-accept').setValue('验收A改')
    await wrapper.get('#revise-target').setValue('t2')

    expect((wrapper.get('#patch-title').element as HTMLInputElement).value).toBe('改过的A')
    expect(wrapper.get('#task-t0').find('form').exists()).toBe(true)

    await wrapper.findAll('button').find((btn) => btn.text() === '保存字段')!.trigger('click')
    await flushPromises()

    expect(resumes.at(-1)?.target_item_id).toBe('t0')
    expect(resumes.at(-1)?.item_patch).toMatchObject({ title: '改过的A', acceptance_criteria: '验收A改' })
    expect((wrapper.get('#revise-target').element as HTMLSelectElement).value).toBe('t2')
    wrapper.unmount()
  })

  it('keeps the other draft after one task is saved', async () => {
    installFetch(() => {
      const rows = items({ t2: '已保存B' })
      return sse([['interrupt', review(rows)], ['result', snap(rows)], ['done', {}]])
    })
    const wrapper = await openReview()
    await clickTask(wrapper, '任务A')
    await wrapper.get('#patch-title').setValue('草稿A')
    await clickTask(wrapper, '任务B')
    await wrapper.get('#patch-title').setValue('已保存B')
    await wrapper.findAll('button').find((btn) => btn.text() === '保存字段')!.trigger('click')
    await flushPromises()

    await clickTask(wrapper, '任务A')
    expect((wrapper.get('#patch-title').element as HTMLInputElement).value).toBe('草稿A')
    await wrapper.findAll('button').find((btn) => btn.text() === '一键批准并完成')!.trigger('click')
    expect(wrapper.text()).toContain('还有未保存的修改')
    expect(wrapper.text()).toContain('任务A：标题')
    expect(resumes.some((body) => body.approve === true)).toBe(false)
    wrapper.unmount()
  })

  it('keeps the typed fields when saving fails', async () => {
    installFetch(() => sse([['error', { message: '保存失败' }], ['done', {}]]))
    const wrapper = await openReview()
    await clickTask(wrapper, '任务B')
    await wrapper.get('#patch-title').setValue('草稿B')
    await clickTask(wrapper, '任务A')
    await wrapper.get('#patch-title').setValue('失败也留着')
    await wrapper.findAll('button').find((btn) => btn.text() === '保存字段')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('保存失败')
    expect((wrapper.get('#patch-title').element as HTMLInputElement).value).toBe('失败也留着')
    await clickTask(wrapper, '任务B')
    expect((wrapper.get('#patch-title').element as HTMLInputElement).value).toBe('草稿B')
    wrapper.unmount()
  })

  it('treats a cleared owner as unassigned on the card, the count, and the sort', async () => {
    installFetch()
    const wrapper = await openReview()
    await clickTask(wrapper, '任务A')
    await wrapper.get('#patch-owner').setValue('')
    await flushPromises()

    const cards = wrapper.findAll('.hitl-review .task-card')
    expect(cards[0].text()).toContain('任务A')
    expect(cards[0].text()).toContain('待分配负责人')
    expect(wrapper.text()).toContain('待分配负责人 1 项')

    await clickTask(wrapper, '任务B')
    await clickTask(wrapper, '任务A')
    expect((wrapper.get('#patch-owner').element as HTMLInputElement).value).toBe('')
    expect(wrapper.findAll('.hitl-review .task-card')[0].text()).toContain('待分配负责人')
    wrapper.unmount()
  })

  it('opens the export file and that file matches the preview', async () => {
    const opened: string[] = []
    vi.spyOn(window, 'open').mockImplementation((url?: string | URL) => {
      opened.push(String(url))
      return null
    })
    vi.stubGlobal('fetch', async (input: RequestInfo) => {
      const url = String(input)
      if (url.includes('/chat/stream')) {
        return sse([
          ['metadata', { thread_id: 'thread-1' }],
          ['result', snap(items(), 'completed')],
          ['done', {}],
        ])
      }
      if (url.includes('/export?format=json')) {
        return new Response(JSON.stringify({ title: '任务A', deadline: '2026-11-04' }))
      }
      if (url.includes('/export?format=md')) return new Response(markdown)
      return new Response('missing', { status: 404 })
    })
    const wrapper = mount(App, { attachTo: document.body })
    await flushPromises()
    await wrapper.get('#meeting-source').setValue('会议原文')
    await wrapper.findAll('button').find((btn) => btn.text() === '生成任务')!.trigger('click')
    await flushPromises()

    await wrapper.findAll('button').find((btn) => btn.text() === '下载 Markdown')!.trigger('click')
    const file = await fetch(opened[0]).then((resp) => resp.text())
    expect(opened[0]).toBe('/api/v1/threads/thread-1/export?format=md')
    expect(file).toBe(markdown)
    await wrapper.get('#result-tab-markdown').trigger('click')
    expect(wrapper.get('.md-preview').text()).toContain('任务A')
    expect(wrapper.get('.md-preview').text()).toContain('2026-11-04')
    expect(file).not.toContain('不应出现的标题')

    await wrapper.findAll('button').find((btn) => btn.text() === '下载 JSON')!.trigger('click')
    const json = await fetch(opened[1]).then((resp) => resp.json())
    expect(opened[1]).toBe('/api/v1/threads/thread-1/export?format=json')
    expect(json).toMatchObject({ title: '任务A', deadline: '2026-11-04' })
    wrapper.unmount()
  })
})
