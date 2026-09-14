/**
 * 结构守卫：分配面板不能整行 @click 定位原文，否则点 <select> 会抢走焦点。
 * 改 App.vue 分配区后应再跑 npm run check:assign（见 package.json）。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const src = fs.readFileSync(path.join(root, 'src', 'App.vue'), 'utf8')

const assignPanel = src.split('interrupt?.type === \'assign_owners\'')[1]?.split('interrupt?.type === \'review\'')[0] || ''
assert.match(assignPanel, /class="assign-locate"/)
assert.match(assignPanel, /class="assign-controls"/)
assert.doesNotMatch(
  assignPanel.replace(/\s+/g, ' '),
  /class="assign-row"[^>]*@click="locateHitlItem/,
  'assign-row must not locate-on-click; that steals focus from the owner select',
)
assert.match(assignPanel, /@click\.stop/)
assert.match(assignPanel, /@mousedown\.stop/)
assert.match(assignPanel, /@change="onPrimaryOwnerPick"/)
assert.match(assignPanel, /@click\.stop="toggleSecondary/)

function isHitlInteractiveTarget(target) {
  if (!target || typeof target.closest !== 'function') return false
  return Boolean(
    target.closest('select, option, button, input, textarea, label, .assign-controls'),
  )
}

assert.equal(isHitlInteractiveTarget({ closest: (sel) => (sel.includes('select') ? {} : null) }), true)
assert.equal(isHitlInteractiveTarget({ closest: () => null }), false)
assert.equal(isHitlInteractiveTarget(null), false)

function locateShouldRun(target, now, suppressUntil) {
  if (isHitlInteractiveTarget(target)) return false
  if (now < suppressUntil) return false
  return true
}

assert.equal(
  locateShouldRun({ closest: (sel) => (sel.includes('select') ? {} : null) }, 1000, 0),
  false,
  'clicking the owner select must not locate source',
)
assert.equal(
  locateShouldRun({ closest: () => null }, 1200, 1400),
  false,
  'click-through after picking an option must not locate source',
)
assert.equal(
  locateShouldRun({ closest: () => null }, 2000, 1400),
  true,
  'clicking the title later should still locate source',
)

console.log('assign-select structure and hit-target guards ok')
