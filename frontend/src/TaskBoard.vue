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
  acceptance_origin?: string
  evidence_kind?: string
  evidence_quote?: string
  inference_reason?: string
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
    <p class="locate-help">点击任务可查看对应原文</p>
    <section v-for="group in groups()" :key="group.name" class="person-col">
      <h4 class="person-name">{{ group.name }}</h4>
      <button
        v-for="item in group.primary"
        :key="item.id + '-p'"
        type="button"
        class="task-card"
        :class="{ selected: selectedId === item.id }"
        @click="emit('select', item)"
      >
        <span v-if="selectedId === item.id" class="current-flag">当前选中</span>
        <span class="role-tag role-primary">主责</span>
        <span class="task-title">{{ item.title }}</span>
        <span v-if="item.deadline" class="line">截止日期：{{ item.deadline }}</span>
        <span v-if="item.acceptance_criteria" class="line">验收标准：{{ item.acceptance_criteria }}</span>
        <span v-if="item.acceptance_origin === 'system'" class="line">验收标准是系统建议</span>
        <span v-if="item.evidence_kind === 'inference'" class="line">推断<span v-if="item.inference_reason">：{{ item.inference_reason }}</span></span>
        <span v-if="selectedId === item.id && item.evidence_quote" class="line quote">依据：{{ item.evidence_quote }}</span>
      </button>
      <button
        v-for="item in group.secondary"
        :key="item.id + '-s'"
        type="button"
        class="task-card secondary"
        :class="{ selected: selectedId === item.id }"
        @click="emit('select', item)"
      >
        <span v-if="selectedId === item.id" class="current-flag">当前选中</span>
        <span class="role-tag role-secondary">次责</span>
        <span class="task-title">{{ item.title }}</span>
        <span class="line">主责：{{ item.primary_owner || '—' }}</span>
        <span v-if="item.acceptance_origin === 'system'" class="line">验收标准是系统建议</span>
        <span v-if="item.evidence_kind === 'inference'" class="line">推断<span v-if="item.inference_reason">：{{ item.inference_reason }}</span></span>
        <span v-if="selectedId === item.id && item.evidence_quote" class="line quote">依据：{{ item.evidence_quote }}</span>
      </button>
    </section>
    <section v-if="unassigned().length" class="person-col unassigned">
      <h4 class="person-name">待分配</h4>
      <button
        v-for="item in unassigned()"
        :key="item.id"
        type="button"
        class="task-card"
        :class="{ selected: selectedId === item.id }"
        @click="emit('select', item)"
      >
        <span v-if="selectedId === item.id" class="current-flag">当前选中</span>
        <span v-if="item.unresolved_ref" class="role-tag role-ref">原文未点名 · {{ item.unresolved_ref }}</span>
        <span class="task-title">{{ item.title }}</span>
        <span v-if="item.evidence_kind === 'inference'" class="line">推断<span v-if="item.inference_reason">：{{ item.inference_reason }}</span></span>
        <span v-if="selectedId === item.id && item.evidence_quote" class="line quote">依据：{{ item.evidence_quote }}</span>
      </button>
    </section>
  </div>
</template>

<style scoped>
.locate-help { font-size: 0.875rem; color: var(--muted); margin-bottom: 0.75rem; }
.person-col { margin-bottom: 1.25rem; }
.person-name { font-size: 1rem; margin-bottom: 0.35rem; }
.task-card {
  display: block;
  width: 100%;
  text-align: left;
  border: none;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  padding: 0.85rem 0.35rem;
  margin-bottom: 0;
  background: transparent;
}
.task-card:hover { background: var(--bg); }
.task-card.selected { background: var(--selected); border-color: var(--primary); }
.task-title, .line { display: block; overflow-wrap: anywhere; }
.task-title { font-size: 1rem; font-weight: 650; margin: 0.35rem 0; color: var(--ink); }
.line { font-size: 0.875rem; color: var(--muted); margin-bottom: 0.2rem; }
.quote { font-size: 0.875rem; }
.current-flag { display: inline-block; margin-right: 0.35rem; color: var(--primary); font-size: 0.875rem; font-weight: 650; }
.role-tag {
  display: inline-block;
  font-size: 0.875rem;
  font-weight: 650;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius-sm);
}
.role-primary { background: var(--ok-bg); color: var(--ok); }
.role-secondary { background: var(--selected); color: var(--primary); }
.role-ref { background: var(--amber-bg); color: var(--amber); }
.unassigned { border-top: 1px solid var(--line); padding-top: 0.75rem; }
</style>
