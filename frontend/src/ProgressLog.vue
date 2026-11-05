<script setup lang="ts">
/** 技术日志默认折叠。普通进度不放在这里。 */
import { nextTick, ref, watch } from 'vue'

interface Entry {
  node: string
  message: string
  kind?: string
  route?: string | null
  status?: string
}

const props = defineProps<{
  entries: Entry[]
  tools: Array<Record<string, unknown>>
}>()

const box = ref<HTMLElement | null>(null)

const KIND: Record<string, string> = {
  model: '模型',
  tool: '工具',
  hitl: '需确认',
  step: '步骤',
}
const STATUS: Record<string, string> = {
  pending: '未开始',
  running: '进行中',
  done: '已完成',
  skipped: '已跳过',
  waiting: '待你操作',
}

function kindLabel(kind?: string) {
  return KIND[kind || ''] || '步骤'
}

function statusLabel(status?: string) {
  if (!status) return ''
  return STATUS[status] || status
}

function routeClass(route: string) {
  if (route === '规则回退') return 'route-rules'
  if (route === 'DeepSeek') return 'route-deepseek'
  if (route === 'DeepSeek + 规则') return 'route-hybrid'
  return 'route-pending'
}

watch(() => props.entries.length + props.tools.length, async () => {
  const el = box.value
  if (!el || el.clientHeight === 0) return
  const near = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  if (!near) return
  await nextTick()
  if (!box.value || box.value.clientHeight === 0) return
  box.value.scrollTop = box.value.scrollHeight
})
</script>

<template>
  <details class="log">
    <summary>查看处理详情</summary>
    <div ref="box" class="progress-list">
      <ul>
        <li v-for="(entry, i) in entries" :key="i">
          <span class="badge">{{ kindLabel(entry.kind) }}</span>
          <span class="mono">{{ entry.node }}</span>
          <span v-if="entry.status">{{ statusLabel(entry.status) }}</span>
          <span v-if="entry.route" class="badge" :class="routeClass(entry.route)">{{ entry.route }}</span>
          <span class="log-msg">{{ entry.message }}</span>
        </li>
      </ul>
      <ul v-if="tools.length" class="tool-list">
        <li v-for="(tool, i) in tools" :key="'t' + i">
          <span class="badge">工具</span>
          <span class="mono">{{ tool.tool || '未命名' }}</span>
        </li>
      </ul>
    </div>
  </details>
</template>

<style scoped>
.log { border-top: 1px solid var(--line); padding-top: 0.75rem; }
summary { cursor: pointer; font-weight: 650; }
.progress-list { max-height: 16rem; overflow: auto; margin-top: 0.75rem; }
ul { list-style: none; }
li {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.5rem;
  align-items: baseline;
  padding: 0.35rem 0;
  font-size: 0.875rem;
}
.log-msg { overflow-wrap: anywhere; color: var(--muted); }
.tool-list { margin-top: 0.5rem; border-top: 1px solid var(--line); padding-top: 0.5rem; }
.route-deepseek, .route-rules, .route-hybrid, .route-pending { background: var(--bg); color: var(--ink); }
</style>
