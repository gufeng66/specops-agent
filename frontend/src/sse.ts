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
  onError?: (data: { message: string }) => void
  onDone?: () => void
}

function parseSSEFrame(frame: string): { event: string; data: string } | null {
  // 一帧里可能有多行 data:，按 SSE 规范拼起来；本项目实际只用单行 JSON。
  let event = 'message'
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data) return null
  return { event, data }
}

export async function streamSSE(url: string, body: unknown, handlers: SSEHandlers): Promise<void> {
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
    })
  } catch (err) {
    const message = err instanceof Error ? err.message : '网络错误'
    handlers.onError?.({ message: `无法连接后端：${message}` })
    return
  }

  if (resp.status === 409) {
    const err = await resp.json()
    handlers.onError?.({ message: err.detail?.message || '线程忙碌，请稍后重试' })
    return
  }

  if (!resp.ok || !resp.body) {
    handlers.onError?.({ message: `HTTP ${resp.status}` })
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // 空行分隔完整帧；最后一段可能半截，留在 buffer 等下次。
      const parts = buffer.split('\n\n')
      buffer = parts.pop() || ''

      for (const frame of parts) {
        const parsed = parseSSEFrame(frame)
        if (!parsed) continue
        let payload: Record<string, unknown> = {}
        try {
          payload = JSON.parse(parsed.data)
        } catch {
          continue
        }
        switch (parsed.event) {
          case 'metadata': handlers.onMetadata?.(payload); break
          case 'progress': handlers.onProgress?.(payload as { node: string; message: string; status?: string }); break
          case 'tool_result': handlers.onToolResult?.(payload); break
          case 'interrupt': handlers.onInterrupt?.(payload); break
          case 'token': handlers.onToken?.(payload as { text: string }); break
          case 'result': handlers.onResult?.(payload); break
          case 'error': handlers.onError?.(payload as { message: string }); break
          case 'done': handlers.onDone?.(); break
        }
      }
    }

    if (buffer.trim()) {
      const parsed = parseSSEFrame(buffer)
      if (parsed) {
        try {
          const payload = JSON.parse(parsed.data)
          if (parsed.event === 'done') handlers.onDone?.()
          else if (parsed.event === 'result') handlers.onResult?.(payload)
        } catch { /* 半截帧忽略 */ }
      }
    }
  } catch (err) {
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
