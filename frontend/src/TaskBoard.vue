<script setup lang="ts">
/**
 * 按人看板。同一任务会同时出现在主责列和次责列；待分配单独成组。
 * 不在卡片上编号，避免和审核「第 N 条」的全局序号打架。
 */
export interface BoardItem {
  id: string
  title: string
  description?: string
  primary_owner: string
  secondary_owners: string[]
  deadline?: string
  acceptance_criteria?: string
  evidence_kind?: string
  evidence_quote?: string
  unresolved_ref?: string
}

interface PersonGroup {
  name: string
  primary: BoardItem[]
  secondary: BoardItem[]
}

const props = defineProps<{
  items: BoardItem[]
  selectedId?: string
}>()

const emit = defineEmits<{
  select: [item: BoardItem]
}>()

function groups(): PersonGroup[] {
  // 按首次出现顺序建列，避免每次渲染看板左右跳动
  const names: string[] = []
  for (const item of props.items) {
    const primary = (item.primary_owner || '').trim()
    if (primary && !names.includes(primary)) names.push(primary)
    for (const name of item.secondary_owners || []) {
      if (name && !names.includes(name)) names.push(name)
    }
  }
  return names.map((name) => ({
    name,
    primary: props.items.filter((item) => (item.primary_owner || '').trim() === name),
    secondary: props.items.filter(
      (item) =>
        (item.secondary_owners || []).includes(name) && (item.primary_owner || '').trim() !== name,
    ),
  })).filter((g) => g.primary.length || g.secondary.length)
}

function unassigned(): BoardItem[] {
  // 没主责的进「待分配」，含代词未消解
  return props.items.filter((item) => !(item.primary_owner || '').trim())
}
</script>

<template>
  <div class="board">
    <p class="locate-help">点击任务可在左侧原文中选中对应句子</p>
    <section v-for="group in groups()" :key="group.name" class="person-col">
      <h4 class="person-name">{{ group.name }}</h4>
      <div
        v-for="item in group.primary"
        :key="item.id + '-p'"
        class="task-card"
        :class="{ selected: selectedId === item.id }"
        @click="emit('select', item)"
      >
        <span class="role-tag role-primary">主责</span>
        <h4>{{ item.title }}</h4>
        <p v-if="item.deadline"><strong>截止：</strong>{{ item.deadline }}</p>
        <p v-if="item.acceptance_criteria"><strong>验收：</strong>{{ item.acceptance_criteria }}</p>
        <p v-if="item.evidence_quote" class="quote">{{ item.evidence_quote }}</p>
      </div>
      <div
        v-for="item in group.secondary"
        :key="item.id + '-s'"
        class="task-card secondary"
        :class="{ selected: selectedId === item.id }"
        @click="emit('select', item)"
      >
        <span class="role-tag role-secondary">次责</span>
        <h4>{{ item.title }}</h4>
        <p><strong>主责：</strong>{{ item.primary_owner || '—' }}</p>
        <p v-if="item.evidence_quote" class="quote">{{ item.evidence_quote }}</p>
      </div>
    </section>
    <section v-if="unassigned().length" class="person-col unassigned">
      <h4 class="person-name">待分配</h4>
      <div
        v-for="item in unassigned()"
        :key="item.id"
        class="task-card"
        :class="{ selected: selectedId === item.id }"
        @click="emit('select', item)"
      >
        <span v-if="item.unresolved_ref" class="role-tag role-ref">原文未点名 · {{ item.unresolved_ref }}</span>
        <h4>{{ item.title }}</h4>
        <p v-if="item.evidence_quote" class="quote">{{ item.evidence_quote }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.locate-help { font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.65rem; }
.person-col { margin-bottom: 1rem; }
.person-name { font-size: 0.95rem; margin-bottom: 0.5rem; }
.task-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.85rem;
  margin-bottom: 0.65rem;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.task-card:hover, .task-card.selected { border-color: var(--accent); }
.task-card.selected { box-shadow: 0 0 0 1px rgba(61, 139, 253, 0.35); }
.task-card.secondary { opacity: 0.92; }
.task-card h4 { font-size: 0.9rem; margin: 0.35rem 0; }
.task-card p { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.2rem; }
.quote { font-size: 0.75rem; color: var(--text-muted); }
.role-tag {
  display: inline-block;
  font-size: 0.68rem;
  font-weight: 700;
  padding: 0.15rem 0.4rem;
  border-radius: 4px;
}
.role-primary { background: rgba(52, 199, 89, 0.18); color: var(--success); }
.role-secondary { background: rgba(61, 139, 253, 0.18); color: #7eb6ff; }
.role-ref { background: rgba(255, 176, 32, 0.2); color: var(--warn); }
.unassigned { border-top: 1px dashed var(--border); padding-top: 0.75rem; }
</style>
