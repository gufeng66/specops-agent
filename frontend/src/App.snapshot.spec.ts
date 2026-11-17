import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'

function items(title = '任务A') {
  return [
    {
      id: 't0',
      index: 1,
      title,
      primary_owner: 'Alice',
      secondary_owners: [],
      evidence_quote: '',
      acceptance_criteria: '验收A',
      deadline: '',
    },
  ]
}

function review() {
  return { type: 'review', interrupt_id: 'int-1', message: '核对任务', items: items() }
}

/** 后端停在待审核时的快照：中断只从快照读，不靠 SSE 事件残留。 */
function snap(over: Record<string, unknown> = {}) {
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
    interrupt_payload: review(),
    ...over,
  }
}

function sse(events: Array<[string, unknown]>) {
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function stubFetch(routes: {
  chat?: () => Response
  resume?: () => Response
  thread?: () => Response | Promise<Response>
}) {
  const calls = { thread: 0 }
  vi.stubGlobal('fetch', async (input: RequestInfo) => {
    const url = String(input)
    if (url.includes('/chat/stream')) return routes.chat ? routes.chat() : sse([['done', {}]])
    if (url.includes('/resume')) return routes.resume ? routes.resume() : sse([['done', {}]])
    if (url.includes('/api/v1/threads/')) {
      calls.thread += 1
      return routes.thread ? routes.thread() : Response.json(snap())
    }
    return new Response('missing', { status: 404 })
  })
  return calls
}

async function openApp(): Promise<VueWrapper> {
  const wrapper = mount(App, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('#meeting-source').setValue('会议原文')
  await wrapper.findAll('button').find((btn) => btn.text() === '生成任务')!.trigger('click')
  await flushPromises()
  return wrapper
}

async function openReview(): Promise<VueWrapper> {
  stubFetch({
    chat: () => sse([
      ['metadata', { thread_id: 'thread-1' }],
      ['result', snap()],
      ['done', {}],
    ]),
  })
  return openApp()
}

function button(wrapper: VueWrapper, label: string) {
  return wrapper.findAll('button').find((btn) => btn.text() === label)
}

describe('snapshot guard', () => {
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

  it('keeps the newer snapshot when another run turns up late', async () => {
    // 同一趟流里后到的 result 属于别的 run：run 校验必须挡住，页面继续显示当前审核。
    stubFetch({
      chat: () => sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap({ revision: 5 })],
        ['result', snap({
          run_id: 'run-old',
          revision: 9,
          status: 'completed',
          interrupt_id: null,
          interrupt_payload: null,
          task_draft: { items: items('旧任务') },
        })],
        ['done', {}],
      ]),
    })
    const wrapper = await openApp()

    expect(wrapper.text()).toContain('审核任务清单')
    expect(wrapper.text()).not.toContain('旧任务')
    expect(wrapper.text()).not.toContain('任务已整理完成')
    wrapper.unmount()
  })

  it('keeps the newer snapshot when the same run sends a lower revision', async () => {
    stubFetch({
      chat: () => sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap({ revision: 4 })],
        ['result', snap({
          revision: 3,
          status: 'completed',
          interrupt_id: null,
          interrupt_payload: null,
          task_draft: { items: items('旧任务') },
          markdown_output: '# 旧结果',
        })],
        ['done', {}],
      ]),
    })
    const wrapper = await openApp()

    expect(wrapper.text()).toContain('审核任务清单')
    expect(wrapper.text()).not.toContain('旧任务')
    wrapper.unmount()
  })

  it('keeps the last snapshot when the refresh fails and offers a retry', async () => {
    const wrapper = await openReview()
    expect(wrapper.text()).toContain('审核任务清单')

    // 批准后服务端没有回 result，页面补查快照；这时网络断了。
    stubFetch({
      resume: () => sse([['done', {}]]),
      thread: () => { throw new TypeError('Failed to fetch') },
    })
    await button(wrapper, '一键批准并完成')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('网络异常，已保留上次结果')
    expect(wrapper.text()).toContain('审核任务清单')
    expect(button(wrapper, '再次查询')).toBeTruthy()

    stubFetch({ thread: () => Response.json(snap({ revision: 2 })) })
    await button(wrapper, '再次查询')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('审核任务清单')
    expect(wrapper.text()).not.toContain('网络异常，已保留上次结果')
    wrapper.unmount()
  })

  it('reloads the server snapshot after an SSE CAS conflict', async () => {
    const wrapper = await openReview()
    const calls = stubFetch({
      resume: () => sse([
        ['error', { message: '服务端状态已更新，请重新查询', code: 'SNAPSHOT_CONFLICT' }],
        ['done', {}],
      ]),
      thread: () => Response.json(snap({
        run_id: 'run-new',
        revision: 2,
        status: 'completed',
        interrupt_id: null,
        interrupt_payload: null,
        markdown_output: '# 服务端最新结果',
      })),
    })
    await button(wrapper, '一键批准并完成')!.trigger('click')
    await flushPromises()

    expect(calls.thread).toBeGreaterThan(0)
    expect(wrapper.text()).toContain('任务已整理完成')
    expect(wrapper.text()).not.toContain('审核任务清单')
    wrapper.unmount()
  })

  it('reports a bad status and unparsable body without dropping the snapshot', async () => {
    const wrapper = await openReview()

    stubFetch({
      resume: () => sse([['done', {}]]),
      thread: () => new Response('boom', { status: 503 }),
    })
    await button(wrapper, '一键批准并完成')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('HTTP 503')
    expect(wrapper.text()).toContain('审核任务清单')

    stubFetch({
      resume: () => sse([['done', {}]]),
      thread: () => new Response('<html>not json</html>', { status: 200 }),
    })
    await button(wrapper, '一键批准并完成')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('响应格式异常')
    expect(wrapper.text()).toContain('审核任务清单')
    wrapper.unmount()
  })

  it('starts only one poller when「再次查询」is clicked repeatedly', async () => {
    const wrapper = await openReview()
    const calls = stubFetch({
      resume: () => sse([['done', {}]]),
      // 一直取不到快照：poller 不会自己收敛，正好暴露并发轮询。
      thread: () => { throw new TypeError('Failed to fetch') },
    })
    await button(wrapper, '一键批准并完成')!.trigger('click')
    await flushPromises()

    const retry = button(wrapper, '再次查询')!
    await retry.trigger('click')
    await retry.trigger('click')
    await retry.trigger('click')
    await flushPromises()
    const afterClicks = calls.thread

    // 等过第一段退避（500ms）。若三个 poller 都在跑，这里会多出三倍请求。
    await new Promise((resolve) => setTimeout(resolve, 1200))
    await flushPromises()
    expect(calls.thread - afterClicks).toBeLessThanOrEqual(3)
    wrapper.unmount()
  })

  it('ignores a late interrupt that claims a newer revision once the run is done', async () => {
    // 完成态优先于一切 interrupt：即便旧帧自称 revision 更大，也不能把页面改回审核，
    // 更不能让它把区块抽取告警之类的审核期状态带进完成页。
    stubFetch({
      chat: () => sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap({
          status: 'completed',
          interrupt_id: null,
          interrupt_payload: null,
          markdown_output: '# 会议纪要',
        })],
        ['interrupt', {
          ...review(),
          interrupt_id: 'int-9',
          revision: 99,
          message: '旧审核',
          extraction: { chunks_total: 3, chunks_ok: 0, chunks_failed: 3, partial: true },
        }],
        ['done', {}],
      ]),
    })
    const wrapper = await openApp()

    expect(wrapper.text()).toContain('任务已整理完成')
    expect(wrapper.text()).not.toContain('审核任务清单')
    expect(wrapper.text()).not.toContain('旧审核')
    expect(wrapper.text()).not.toContain('本次有 3 个分块抽取失败')
    wrapper.unmount()
  })

  it('drops an interrupt from another interrupt id while review is still open', async () => {
    // 审核面板已经绑在 int-1 上，另一份中断没有更新证据，不能顶掉当前这一份。
    stubFetch({
      chat: () => sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap()],
        ['interrupt', { ...review(), interrupt_id: 'int-old', items: items('旧中断任务'), message: '旧中断' }],
        ['done', {}],
      ]),
    })
    const wrapper = await openApp()

    expect(wrapper.text()).toContain('审核任务清单')
    expect(wrapper.text()).toContain('任务A')
    expect(wrapper.text()).not.toContain('旧中断任务')
    expect(wrapper.text()).not.toContain('旧中断')
    wrapper.unmount()
  })

  it('still renders the review panel when the interrupt matches the snapshot', async () => {
    // 快照只说明在等审核、没带 payload 时，同 id 的 interrupt 帧仍要能把面板撑起来。
    stubFetch({
      chat: () => sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap({ interrupt_payload: null })],
        ['interrupt', review()],
        ['done', {}],
      ]),
    })
    const wrapper = await openApp()

    expect(wrapper.text()).toContain('审核任务清单')
    expect(button(wrapper, '一键批准并完成')).toBeTruthy()
    wrapper.unmount()
  })

  it('clears the interrupt as soon as the run leaves awaiting_review', async () => {
    stubFetch({
      chat: () => sse([
        ['metadata', { thread_id: 'thread-1' }],
        ['result', snap({
          status: 'completed',
          interrupt_id: null,
          interrupt_payload: null,
          markdown_output: '# 会议纪要',
        })],
        ['interrupt', review()],
        ['done', {}],
      ]),
    })
    const wrapper = await openApp()

    // 快照说已完成，后到的 interrupt 事件不能让页面回到审核状态。
    expect(wrapper.text()).toContain('任务已整理完成')
    expect(wrapper.text()).not.toContain('审核任务清单')
    expect(wrapper.text()).not.toContain('一键批准并完成')
    wrapper.unmount()
  })
})
