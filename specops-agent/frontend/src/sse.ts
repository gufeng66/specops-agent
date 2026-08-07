export interface SSEHandlers {
  onMetadata?: (data: Record<string, unknown>) => void
  onProgress?: (data: { node: string; message: string }) => void
  onToolResult?: (data: Record<string, unknown>) => void
  onInterrupt?: (data: Record<string, unknown>) => void
  onToken?: (data: { text: string }) => void
  onResult?: (data: Record<string, unknown>) => void
  onError?: (data: { message: string }) => void
  onDone?: () => void
}

function parseSSEFrame(frame: string): { event: string; data: string } | null {
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
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

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

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

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
        case 'progress': handlers.onProgress?.(payload as { node: string; message: string }); break
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
      } catch { /* ignore partial */ }
    }
  }
}

export function uuid(): string {
  return crypto.randomUUID()
}
