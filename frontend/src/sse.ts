/**
 * SSE 客户端：浏览器 EventSource 只支持 GET，这里用 fetch + ReadableStream
 * 解析 `event:` / `data:` 帧，把后端 events.py 的事件分发给 App.vue。
 *
 * 409 表示线程忙碌（THREAD_BUSY）。request_id 由 uuid() 生成，用于后端幂等；
 * 每次点击都会新生成，所以连点两次「开始分析」不是幂等重放。
 */
export interface SSEHandlers {
  onMetadata?: (data: Record<string, unknown>) => void
  onProgress?: (data: { node: string; message: string; status?: string }) => void
  onToolResult?: (data: Record<string, unknown>) => void
  onInterrupt?: (data: Record<string, unknown>) => void
  onToken?: (data: { text: string }) => void
  onResult?: (data: Record<string, unknown>) => void
  onError?: (data: { message: string; code?: string; thread_id?: string; run_id?: string }) => void
  onDone?: () => void
}

/** 一帧的解析结果。data 已按 SSE 规范拼好，但还没 JSON.parse。 */
export interface SSEFrame {
  event: string
  data: string
}

type FrameSink = (frame: SSEFrame) => void

/**
 * 逐行累加的帧解析器。
 *
 * 不自己切 `\n\n`：分包边界可能正好落在 '\r' 和 '\n' 之间、'data:' 中间，
 * 或者末尾只剩半个 UTF-8 字符。这里只在拿到完整换行时才吐出一行，
 * 半截行留在 buffer 里等下一段；流结束时用 final=true 冲掉最后一帧。
 */
export class SSELines {
  private buffer = ''
  private event = 'message'
  private data: string[] = []

  constructor(private readonly sink: FrameSink) {}

  push(text: string) {
    this.buffer += text
    for (const line of this.takeLines(false)) this.feedLine(line)
  }

  /** 流结束：处理残留的半行，并把最后没有空行收尾的一帧发出去。 */
  end() {
    for (const line of this.takeLines(true)) this.feedLine(line)
    if (this.buffer) {
      this.feedLine(this.buffer)
      this.buffer = ''
    }
    this.flush()
  }

  private takeLines(final: boolean): string[] {
    // 换行有三种：\r\n、\r、\n。'\r' 在缓冲末尾时可能是下一段开头 '\n' 的前半，
    // 不能急着当行尾，否则会多切出一个空行、把一帧提前结束。
    const lines: string[] = []
    let start = 0
    let i = 0
    while (i < this.buffer.length) {
      const ch = this.buffer[i]
      if (ch === '\n') {
        lines.push(this.buffer.slice(start, i))
        i += 1
        start = i
        continue
      }
      if (ch === '\r') {
        if (i + 1 >= this.buffer.length && !final) break
        lines.push(this.buffer.slice(start, i))
        i += this.buffer[i + 1] === '\n' ? 2 : 1
        start = i
        continue
      }
      i += 1
    }
    this.buffer = this.buffer.slice(start)
    return lines
  }

  private feedLine(line: string) {
    if (!line) {
      // 空行结束一帧。
      this.flush()
      return
    }
    if (line.startsWith(':')) return // 注释/心跳
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') this.event = value || 'message'
    else if (field === 'data') this.data.push(value)
    // id / retry 属于断线重连语义，本项目每次都重新发起请求，忽略即可。
  }

  private flush() {
    if (!this.data.length) {
      this.event = 'message'
      return
    }
    const frame = { event: this.event, data: this.data.join('\n') }
    this.event = 'message'
    this.data = []
    this.sink(frame)
  }
}

/**
 * 统一分发。循环里出来的帧和流结束时冲出来的尾帧走同一条路，
 * 尾帧是 error / interrupt 时也不会被丢掉（以前只认 done 和 result）。
 */
export function dispatchSSE(frame: SSEFrame, handlers: SSEHandlers): void {
  if (!frame.data) return
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(frame.data)
  } catch {
    return // 半截 JSON 无法恢复，丢掉这一帧
  }
  switch (frame.event) {
    case 'metadata': handlers.onMetadata?.(payload); break
    case 'progress': handlers.onProgress?.(payload as { node: string; message: string; status?: string }); break
    case 'tool_result': handlers.onToolResult?.(payload); break
    case 'interrupt': handlers.onInterrupt?.(payload); break
    case 'token': handlers.onToken?.(payload as { text: string }); break
    case 'result': handlers.onResult?.(payload); break
    case 'error': handlers.onError?.(payload as { message: string; code?: string; thread_id?: string; run_id?: string }); break
    case 'done': handlers.onDone?.(); break
  }
}

export async function streamSSE(
  url: string,
  body: unknown,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let resp: Response
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        'Cache-Control': 'no-cache',
      },
      body: JSON.stringify(body),
      cache: 'no-store',
      signal,
    })
  } catch (err) {
    if (signal?.aborted) return
    const message = err instanceof Error ? err.message : '网络错误'
    handlers.onError?.({ message: `无法连接后端：${message}` })
    return
  }

  if (resp.status === 409) {
    let message = '线程忙碌，请稍后重试'
    try {
      const err = await resp.json()
      message = err?.detail?.message || message
    } catch { /* 409 的响应体不是 JSON 时保留默认文案 */ }
    handlers.onError?.({ message })
    return
  }

  if (!resp.ok || !resp.body) {
    handlers.onError?.({ message: `HTTP ${resp.status}` })
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  const lines = new SSELines((frame) => dispatchSSE(frame, handlers))

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      lines.push(decoder.decode(value, { stream: true }))
    }
    // 不带 stream 再解码一次，把最后可能剩下的多字节字符补完；否则末尾半个
    // 汉字会让最后那行变成乱码，整帧 JSON 解析失败。
    lines.push(decoder.decode())
    lines.end()
  } catch (err) {
    if (signal?.aborted) return
    const message = err instanceof Error ? err.message : '流读取失败'
    handlers.onError?.({ message })
  }
}

/** 优先 Web Crypto；无 randomUUID 时拼 UUID v4，保证 request_id 始终可生成。 */
export function uuid(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID()
  }
  const bytes = new Uint8Array(16)
  if (c && typeof c.getRandomValues === 'function') {
    c.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
