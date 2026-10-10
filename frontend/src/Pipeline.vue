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
      <span class="hint">{{ statuses[node.id] === 'waiting' ? '待你操作' : (statuses[node.id] || 'pending') }}</span>
      <span v-if="idx < NODES.length - 1" class="connector" />
    </li>
  </ol>
</template>

<style scoped>
.pipeline {
  display: flex;
  flex-wrap: wrap;
  gap: 0.15rem 0;
  list-style: none;
  padding: 0.25rem 0 0;
}
.pipe-node {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.75rem;
  color: var(--text-muted);
  position: relative;
}
.dot {
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 50%;
  background: #3a4654;
  flex-shrink: 0;
}
.label { font-weight: 600; }
.hint {
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 0.75rem;
}
.connector {
  width: 1.1rem;
  height: 1px;
  background: var(--border);
  margin: 0 0.35rem;
}
.st-running { color: var(--accent); }
.st-running .dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px rgba(61, 139, 253, 0.25);
}
.st-done { color: var(--success); }
.st-done .dot { background: var(--success); }
.st-skipped { color: var(--text-muted); opacity: 0.55; }
.st-skipped .dot { background: #4a5563; }
.st-waiting { color: var(--warn); }
.st-waiting .dot {
  background: var(--warn);
  box-shadow: 0 0 0 3px rgba(255, 176, 32, 0.25);
}
</style>
