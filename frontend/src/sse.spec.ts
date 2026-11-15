import { afterEach, describe, expect, it, vi } from 'vitest'
import { dispatchSSE, SSELines, streamSSE, type SSEFrame, type SSEHandlers } from './sse'

function collect() {
  const frames: SSEFrame[] = []
  const lines = new SSELines((frame) => frames.push(frame))
  const push = (text: string) => lines.push(text)
  const end = () => lines.end()
  return { frames, push, end }
}

/** 把字符串按字节切成若干段，用来模拟网络分包。 */
function bytes(text: string): Uint8Array {
  return new TextEncoder().encode(text)
}

function stream(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
}

describe('SSE 帧解析', () => {
  it('把事件和数据拼成一帧', () => {
    const { frames, push } = collect()
    push('event: result\ndata: {"a":1}\n\n')
    expect(frames).toEqual([{ event: 'result', data: '{"a":1}' }])
  })

  it('接受 CRLF 与单独的 CR 换行', () => {
    const crlf = collect()
    crlf.push('event: done\r\ndata: {}\r\n\r\n')
    expect(crlf.frames).toEqual([{ event: 'done', data: '{}' }])

    // 单独 '\r' 结尾时最后一个 '\r' 可能是下一段 '\n' 的前半，先压住；
    // 后面来了别的字节（这里是一帧内容）就能确认它确实是行尾。
    const cr = collect()
    cr.push('event: done\rdata: {}\r')
    expect(cr.frames).toEqual([])
    cr.push('\r')
    expect(cr.frames).toEqual([])
    cr.push('event: result\rdata: {"ok":1}\r\r')
    expect(cr.frames).toEqual([{ event: 'done', data: '{}' }])
    cr.end()
    expect(cr.frames).toEqual([
      { event: 'done', data: '{}' },
      { event: 'result', data: '{"ok":1}' },
    ])
  })

  it('分包落在 \\r\\n 中间时不提前结束帧', () => {
    const { frames, push } = collect()
    push('event: progress\r')
    // 此刻还看不到 '\n'，'event' 行应该已经切出来了，但帧还没结束。
    expect(frames).toEqual([])
    push('\ndata: {"node":"intake"}\r')
    expect(frames).toEqual([])
    push('\n\r\n')
    expect(frames).toEqual([{ event: 'progress', data: '{"node":"intake"}' }])
  })

  it('半包到达时等下一段补齐', () => {
    const { frames, push } = collect()
    push('event: res')
    expect(frames).toEqual([])
    push('ult\ndata: {"x":')
    expect(frames).toEqual([])
    push('1}\n\n')
    expect(frames).toEqual([{ event: 'result', data: '{"x":1}' }])
  })

  it('多行 data: 按换行拼起来', () => {
    const { frames, push } = collect()
    push('event: token\ndata: {"text":"第一行"}\ndata: \ndata: 尾巴\n\n')
    expect(frames).toEqual([{ event: 'token', data: '{"text":"第一行"}\n\n尾巴' }])
  })

  it('忽略注释行和没有 data 的空事件', () => {
    const { frames, push } = collect()
    push(': keep-alive\n\nevent: ping\n\nevent: done\ndata: {}\n\n')
    expect(frames).toEqual([{ event: 'done', data: '{}' }])
  })

  it('缺省 event 就是 message，冒号后可无空格', () => {
    const { frames, push } = collect()
    push('data:{"a":1}\n\n')
    expect(frames).toEqual([{ event: 'message', data: '{"a":1}' }])
  })

  it('流结束时冲出没有空行收尾的尾帧', () => {
    const { frames, push, end } = collect()
    push('event: done\ndata: {}')
    expect(frames).toEqual([])
    end()
    expect(frames).toEqual([{ event: 'done', data: '{}' }])
  })

  it('尾帧带完整 event/data 时也能被识别', () => {
    const { frames, push, end } = collect()
    push('event: error\ndata: {"message":"后端炸了"}\n\n')
    push('event: interrupt\ndata: {"type":"review","interrupt_id":"int-1"}')
    end()
    expect(frames.map((f) => f.event)).toEqual(['error', 'interrupt'])
  })
})

describe('SSE 分发', () => {
  it('尾帧走同一条分发路径，error / interrupt 不会被丢掉', () => {
    const seen: string[] = []
    const handlers: SSEHandlers = {
      onInterrupt: (d) => seen.push(`interrupt:${String(d.interrupt_id)}`),
      onError: (d) => seen.push(`error:${d.message}`),
      onResult: () => seen.push('result'),
      onDone: () => seen.push('done'),
    }
    dispatchSSE({ event: 'interrupt', data: '{"type":"review","interrupt_id":"int-1"}' }, handlers)
    dispatchSSE({ event: 'error', data: '{"message":"失败"}' }, handlers)
    dispatchSSE({ event: 'done', data: '{}' }, handlers)
    expect(seen).toEqual(['interrupt:int-1', 'error:失败', 'done'])
  })

  it('保留服务端冲突错误码，供页面回读权威快照', () => {
    const seen: Array<{ message: string; code?: string }> = []
    dispatchSSE(
      { event: 'error', data: '{"message":"请重新查询","code":"SNAPSHOT_CONFLICT"}' },
      { onError: (data) => seen.push(data) },
    )
    expect(seen).toEqual([{ message: '请重新查询', code: 'SNAPSHOT_CONFLICT' }])
  })

  it('非 JSON 数据直接丢弃，不打断后面的事件', () => {
    const seen: string[] = []
    dispatchSSE({ event: 'result', data: '{"半截' }, { onResult: () => seen.push('result') })
    dispatchSSE({ event: 'done', data: '{}' }, { onDone: () => seen.push('done') })
    expect(seen).toEqual(['done'])
  })
})

describe('streamSSE 读流', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('末尾半个 UTF-8 字节在 flush 后补全', async () => {
    const body = 'event: result\ndata: {"title":"需求评审"}\n\n'
    const all = bytes(body)
    // 在最后一个汉字中间切一刀：'审' 是 3 字节，这里留下 1 字节。
    const cut = all.length - 8
    vi.stubGlobal('fetch', async () => new Response(stream([all.slice(0, cut), all.slice(cut)]), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }))

    const results: Array<Record<string, unknown>> = []
    await streamSSE('/x', {}, { onResult: (d) => results.push(d) })
    expect(results).toEqual([{ title: '需求评审' }])
  })

  it('跨 chunk 的多字节字符不会被截成乱码', async () => {
    const body = 'event: result\ndata: {"title":"会议"}\n\n'
    const all = bytes(body)
    const cut = all.length - 4
    vi.stubGlobal('fetch', async () => new Response(stream([
      all.slice(0, cut),
      all.slice(cut, cut + 2),
      all.slice(cut + 2),
    ]), { status: 200 }))

    const results: Array<Record<string, unknown>> = []
    await streamSSE('/x', {}, { onResult: (d) => results.push(d) })
    expect(results).toEqual([{ title: '会议' }])
  })

  it('CRLF 分包下事件不丢', async () => {
    const body = [
      'event: metadata\r\ndata: {"thread_id":"t1"}\r\n\r\n',
      'event: interrupt\r\ndata: {"type":"review"}\r\n\r\n',
      'event: done\r\ndata: {}\r\n\r\n',
    ].join('')
    const all = bytes(body)
    const chunks: Uint8Array[] = []
    for (let i = 0; i < all.length; i += 7) chunks.push(all.slice(i, i + 7))
    vi.stubGlobal('fetch', async () => new Response(stream(chunks), { status: 200 }))

    const seen: string[] = []
    await streamSSE('/x', {}, {
      onMetadata: (d) => seen.push(`metadata:${String(d.thread_id)}`),
      onInterrupt: (d) => seen.push(`interrupt:${String(d.type)}`),
      onDone: () => seen.push('done'),
    })
    expect(seen).toEqual(['metadata:t1', 'interrupt:review', 'done'])
  })

  it('非 2xx 与不可读响应体都走 onError', async () => {
    const messages: string[] = []
    vi.stubGlobal('fetch', async () => new Response('nope', { status: 500 }))
    await streamSSE('/x', {}, { onError: (d) => messages.push(d.message) })
    expect(messages).toEqual(['HTTP 500'])
  })

  it('409 优先用后端给的忙碌文案', async () => {
    const messages: string[] = []
    vi.stubGlobal('fetch', async () => Response.json(
      { detail: { message: '线程正在执行另一趟' } },
      { status: 409 },
    ))
    await streamSSE('/x', {}, { onError: (d) => messages.push(d.message) })
    expect(messages).toEqual(['线程正在执行另一趟'])
  })

  it('409 响应体不是 JSON 时保留默认文案', async () => {
    const messages: string[] = []
    vi.stubGlobal('fetch', async () => new Response('busy', { status: 409 }))
    await streamSSE('/x', {}, { onError: (d) => messages.push(d.message) })
    expect(messages).toEqual(['线程忙碌，请稍后重试'])
  })

  it('网络错误拼上原因，abort 时保持安静', async () => {
    const messages: string[] = []
    vi.stubGlobal('fetch', async () => { throw new Error('socket hang up') })
    await streamSSE('/x', {}, { onError: (d) => messages.push(d.message) })
    expect(messages).toEqual(['无法连接后端：socket hang up'])

    const ctrl = new AbortController()
    ctrl.abort()
    const quiet: string[] = []
    vi.stubGlobal('fetch', async () => { throw new DOMException('aborted', 'AbortError') })
    await streamSSE('/x', {}, { onError: (d) => quiet.push(d.message) }, ctrl.signal)
    expect(quiet).toEqual([])
  })
})
