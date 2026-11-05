<script setup lang="ts">
/**
 * 链路步进条。id 必须与后端 WORKFLOW_NODES / SSE progress.node 一致。
 * skipped 用 .st-skipped 变淡，不要画成 running。
 */
const NODES = [
  { id: 'intake', label: '解析' },
  { id: 'clarify', label: '澄清' },
  { id: 'decompose', label: '拆解' },
  { id: 'validate_repair', label: '校验' },
  { id: 'propose_owners', label: '建议' },
  { id: 'assign_owners', label: '分配' },
  { id: 'review', label: '审核' },
  { id: 'finalize', label: '导出' },
] as const

export type NodeStatus = 'pending' | 'running' | 'done' | 'skipped' | 'waiting'

defineProps<{
  statuses: Record<string, NodeStatus>
}>()

const HINT: Record<string, string> = {
  pending: '未开始',
  running: '进行中',
  done: '已完成',
  skipped: '已跳过',
  waiting: '待你操作',
}

function hint(status?: string) {
  return HINT[status || 'pending'] || status || '未开始'
}
</script>

<template>
  <ol class="pipeline">
    <li
      v-for="(node, idx) in NODES"
      :key="node.id"
      class="pipe-node"
      :class="'st-' + (statuses[node.id] || 'pending')"
    >
      <span class="dot" />
      <span class="label">{{ node.label }}</span>
      <span class="hint">{{ hint(statuses[node.id]) }}</span>
      <span v-if="idx < NODES.length - 1" class="connector" />
    </li>
  </ol>
</template>

<style scoped>
.pipeline {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.75rem;
  list-style: none;
  padding: 0.25rem 0 0;
}
.pipe-node {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.875rem;
  color: var(--muted);
  position: relative;
}
.dot {
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 50%;
  background: var(--line);
  flex-shrink: 0;
}
.label { font-weight: 650; color: var(--ink); }
.hint { font-size: 0.875rem; }
.connector {
  width: 1rem;
  height: 1px;
  background: var(--line);
  margin-left: 0.15rem;
}
.st-running { color: var(--primary); }
.st-running .dot { background: var(--primary); }
.st-running .label { color: var(--primary); }
.st-done .dot { background: var(--ok); }
.st-skipped { opacity: 0.7; }
.st-waiting { color: var(--amber); }
.st-waiting .dot { background: var(--amber); }
.st-waiting .label { color: var(--amber); }
</style>
