<script setup lang="ts">
/**
 * 会议原文。编辑和定位分两套视图，避免高亮层和输入框错位。
 */
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { cycleIndex } from './session'

const text = defineModel<string>('text', { required: true })

const props = defineProps<{
  mode: 'edit' | 'read'
  expanded: boolean
  drawer: boolean
  open: boolean
  parts: { before: string; hit: string; after: string } | null
  hint: string
  softLimit: number
  hardLimit: number
}>()

const emit = defineEmits<{
  edit: []
  'update:expanded': [value: boolean]
  close: []
}>()

const sourceRead = ref<HTMLElement | null>(null)
const pane = ref<HTMLElement | null>(null)
const expandBtn = ref<HTMLButtonElement | null>(null)

function focusables(root: HTMLElement) {
  return [...root.querySelectorAll<HTMLElement>('button, textarea, [tabindex="0"]')].filter((el) => {
    if ((el as HTMLButtonElement).disabled) return false
    return el.getClientRects().length > 0
  })
}

function centerOn(scroller: HTMLElement, target: HTMLElement) {
  const box = scroller.getBoundingClientRect()
  const rect = target.getBoundingClientRect()
  scroller.scrollTop += rect.top + rect.height / 2 - (box.top + box.height / 2)
}

function isScrollable(el: HTMLElement): boolean {
  if (el.scrollHeight <= el.clientHeight + 1) return false
  return /(auto|scroll)/.test(getComputedStyle(el).overflowY)
}

function scrollToMark() {
  // 只滚动原文自己的滚动容器，不用 scrollIntoView：后者会连带把整页滚走，
  // 用户点一下定位，左侧任务列表也跟着位移，回不到原来的位置。
  const mark = sourceRead.value?.querySelector('mark')
  if (!(mark instanceof HTMLElement)) return
  const scrollers: HTMLElement[] = []
  if (sourceRead.value && isScrollable(sourceRead.value)) scrollers.push(sourceRead.value)
  for (let el = mark.parentElement; el && el !== document.body; el = el.parentElement) {
    if (isScrollable(el)) scrollers.push(el)
  }
  // 由内到外依次校正，外层滚动不会破坏内层已算好的居中位置。
  for (const scroller of scrollers) centerOn(scroller, mark)
}

function trapFocus(event: KeyboardEvent) {
  if (!props.expanded || event.key !== 'Tab' || !pane.value) return
  const items = focusables(pane.value)
  if (!items.length) return
  const index = items.indexOf(document.activeElement as HTMLElement)
  if (index < 0) {
    event.preventDefault()
    items[0].focus()
    return
  }
  const edge = event.shiftKey ? index === 0 : index === items.length - 1
  if (!edge) return
  event.preventDefault()
  items[cycleIndex(index, items.length, event.shiftKey)].focus()
}

function setBackgroundInert(on: boolean) {
  const app = document.getElementById('app')
  if (app) app.inert = on
}

watch(() => props.expanded, async (on) => {
  setBackgroundInert(on)
  await nextTick()
  if (on && pane.value) focusables(pane.value)[0]?.focus()
  else expandBtn.value?.focus()
})

// 放大、抽屉切换都会改变容器尺寸，之前算好的滚动位置就失效了，要重新居中。
// 等两帧是为了让 transition / Teleport 之后的布局真正落定。
watch([() => props.expanded, () => props.drawer, () => props.open], async () => {
  await nextTick()
  await nextTick()
  scrollToMark()
})

onUnmounted(() => setBackgroundInert(false))

defineExpose({ scrollToMark })
</script>

<template>
  <Teleport to="body" :disabled="!expanded">
    <div
      ref="pane"
      class="source-pane"
      :class="{ expanded, 'is-open': open }"
      :role="expanded ? 'dialog' : undefined"
      :aria-modal="expanded ? true : undefined"
      :aria-label="expanded ? '会议原文' : undefined"
      @keydown="trapFocus"
    >
      <div v-if="drawer" class="drawer-bar">
        <p>会议原文</p>
        <button type="button" class="btn-ghost" @click="emit('close')">关闭原文</button>
      </div>
      <label class="field-label" for="meeting-source">会议原文</label>
      <textarea
        v-show="mode === 'edit'"
        id="meeting-source"
        v-model="text"
        placeholder="在这里粘贴会议记录"
      />
      <div v-show="mode === 'read'" ref="sourceRead" class="source-read" tabindex="0">
        <template v-if="parts">{{ parts.before }}<mark>{{ parts.hit }}</mark>{{ parts.after }}</template>
        <template v-else>{{ text }}</template>
      </div>
      <p v-if="hint" class="locate-hint" role="status">{{ hint }}</p>
      <p class="char-count">
        {{ text.length }} 字
        <span v-if="text.length > softLimit && text.length <= hardLimit">，原文较长，生成会更慢</span>
        <span v-else-if="text.length > hardLimit">，已超过上限，无法生成</span>
      </p>
      <div class="source-toolbar">
        <button v-if="mode === 'read'" class="btn-ghost" type="button" @click="emit('edit')">编辑原文</button>
        <button ref="expandBtn" class="btn-ghost" type="button" @click="emit('update:expanded', !expanded)">
          {{ expanded ? '收起' : '放大原文' }}
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.source-pane { min-width: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.field-label { font-size: 0.875rem; color: var(--muted); }
.source-read {
  min-height: 8rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  flex: 0 1 auto;
  /* 面板高度被 max-height 压住时，长原文必须在这里自己滚，
     否则文字会溢出白框、盖住下面的字数和工具栏按钮。 */
  overflow-y: auto;
  overscroll-behavior: contain;
  font-family: var(--font);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  color: var(--ink);
  padding: 0.85rem 1rem;
  line-height: var(--source-line);
}
.source-read mark { background: var(--mark); color: inherit; }
.locate-hint { color: var(--amber); font-size: 0.875rem; }
.char-count { color: var(--muted); font-size: 0.875rem; }
.source-toolbar { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.drawer-bar { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
.drawer-bar p { font-weight: 650; }
.expanded {
  position: fixed;
  inset: 4vh 5vw;
  z-index: 60;
  background: var(--bg);
  padding: 1rem;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: auto;
}
.expanded textarea,
.expanded .source-read { flex: 1 1 auto; min-height: 0; max-height: none; }
</style>
