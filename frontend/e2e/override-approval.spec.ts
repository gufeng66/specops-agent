import { expect, test, type Page } from '@playwright/test'

/**
 * 阻断错误下的放行流程：审核面板 → 填写理由 → 提交 → 完成页 Markdown 留下放行记录。
 * 全部用 page.route 构造快照，不依赖后端真的产出非法证据（正常流程不会生成阻断错误）。
 */

const BLOCKING = {
  code: 'INVALID_EVIDENCE_QUOTE',
  message: '依据原文无法在会议记录中找到，请补充或改为推断',
  severity: 'error',
  item_id: 't0',
  field: 'evidence_quote',
}

function items() {
  return [
    {
      id: 't0',
      index: 1,
      title: '补充灰度开关文档',
      primary_owner: 'Alice',
      secondary_owners: [],
      evidence_quote: '这句话原文里没有',
      acceptance_criteria: '文档评审通过',
      deadline: '2026-11-20',
    },
  ]
}

function reviewPayload() {
  return {
    type: 'review',
    interrupt_id: 'int-1',
    message: '核对任务',
    items: items(),
    unresolved_errors: [BLOCKING],
    advisory_issues: [],
    blocking_errors: [BLOCKING],
    extraction: {
      chunks_total: 2,
      chunks_ok: 2,
      chunks_empty: 0,
      chunks_failed: 0,
      partial: false,
      degraded: false,
      detail: '',
    },
  }
}

function awaitingSnapshot() {
  return {
    thread_id: 'thread-1',
    run_id: 'run-1',
    status: 'awaiting_review',
    revision: 1,
    source_text: '会议原文：灰度开关要补文档。',
    meeting_brief: { goal: '补齐灰度能力', stakeholders: ['Alice'], meeting_date: '2026-11-13' },
    task_draft: { items: items() },
    validation: { issues: [BLOCKING] },
    markdown_output: '',
    interrupt_id: 'int-1',
    interrupt_payload: reviewPayload(),
    extraction: {
      chunks_total: 2,
      chunks_ok: 2,
      chunks_empty: 0,
      chunks_failed: 0,
      partial: false,
      degraded: false,
      detail: '',
    },
  }
}

/** 后端导出时的 Markdown 结构：问题清单在最前，人工放行段单独一节。 */
const markdownWithOverride = [
  '# 会议任务',
  '',
  '1. **补充灰度开关文档** · 2026-11-20',
  '',
  '## 未解决问题',
  '',
  `- ❌ [${BLOCKING.code}] ${BLOCKING.message}`,
  '',
  '## 人工放行',
  '',
  '- 放行人（自述，未认证）：李娜',
  '- 放行时间：2026-11-13T10:00:00',
  '- 放行理由：原文口语表达，人工确认依据可用',
  `- 放行问题码：${BLOCKING.code}`,
  '',
].join('\n')

function completedSnapshot(overrideReason: string, claim: string) {
  return {
    thread_id: 'thread-1',
    run_id: 'run-1',
    status: 'completed',
    revision: 2,
    source_text: '会议原文：灰度开关要补文档。',
    meeting_brief: { goal: '补齐灰度能力', stakeholders: ['Alice'], meeting_date: '2026-11-13' },
    task_draft: { items: items() },
    validation: { issues: [BLOCKING] },
    markdown_output: markdownWithOverride,
    interrupt_id: null,
    interrupt_payload: null,
    review: {
      override_reason: overrideReason,
      override_codes: [BLOCKING.code],
      override_at: '2026-11-13T10:00:00',
      operator_claim: claim,
    },
  }
}

function sse(events: Array<[string, unknown]>) {
  return events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
}

async function stubApp(page: Page, bodies: Array<Record<string, unknown>>) {
  await page.route('**/api/v1/chat/stream', (route) =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: sse([
        ['metadata', { thread_id: 'thread-1', run_id: 'run-1' }],
        ['result', awaitingSnapshot()],
        ['interrupt', reviewPayload()],
        ['done', {}],
      ]),
    }),
  )
  await page.route('**/api/v1/runs/*/resume', (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>
    bodies.push(body)
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: sse([
        ['metadata', { thread_id: 'thread-1', run_id: 'run-1' }],
        [
          'result',
          completedSnapshot(String(body.override_reason || ''), String(body.operator_claim || '')),
        ],
        ['done', {}],
      ]),
    })
  })
  await page.route('**/api/v1/threads/*', (route) => route.fulfill({ json: awaitingSnapshot() }))
}

test('阻断错误必须填理由才能放行，放行记录进入完成的 Markdown', async ({ page }) => {
  const bodies: Array<Record<string, unknown>> = []
  await stubApp(page, bodies)

  await page.goto('/')
  await page.getByLabel('会议原文').fill('会议原文：灰度开关要补文档。')
  await page.getByRole('button', { name: '生成任务' }).click()

  // 1. 审核面板展示阻断错误，普通批准不可用。
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 30_000 })
  const approve = page.getByRole('button', { name: '一键批准并完成' })
  const blocking = page.locator('.issue-blocking')
  await expect(blocking).toBeVisible()
  await expect(blocking).toContainText('有 1 项必须先处理或显式放行')
  await expect(blocking).toContainText(BLOCKING.message)
  await expect(page.locator('#override-form')).toBeVisible()
  await expect(approve).toBeDisabled()

  // 2. 空白理由不算填过，按钮仍然不可用。
  await page.locator('#override-reason').fill('   ')
  await expect(approve).toBeDisabled()

  // 3. 填理由 + 自述放行人后放行。
  await page.locator('#override-reason').fill('原文口语表达，人工确认依据可用')
  await page.locator('#operator-claim').fill('李娜')
  await expect(approve).toBeEnabled()
  await approve.click()

  // 4. 请求体带上放行字段，且是 approve 提交。
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0].approve).toBe(true)
  expect(bodies[0].override_reason).toBe('原文口语表达，人工确认依据可用')
  expect(bodies[0].operator_claim).toBe('李娜')
  expect(bodies[0].interrupt_id).toBe('int-1')

  // 5. 完成页的 Markdown 预览留下问题清单与人工放行记录。
  await expect(page.getByRole('heading', { name: '任务已整理完成' })).toBeVisible({ timeout: 30_000 })
  await page.getByRole('tab', { name: 'Markdown' }).click()
  const preview = page.locator('.md-preview')
  await expect(preview).toContainText('未解决问题')
  await expect(preview).toContainText('人工放行')
  await expect(preview).toContainText('放行理由')
  await expect(preview).toContainText('原文口语表达，人工确认依据可用')
  await expect(preview).toContainText(`放行问题码：${BLOCKING.code}`)
  await expect(preview).toContainText('放行人（自述，未认证）：李娜')
})

test('没有阻断错误时不出现放行表单，普通批准直接放行', async ({ page }) => {
  const bodies: Array<Record<string, unknown>> = []
  await page.route('**/api/v1/chat/stream', (route) =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: sse([
        ['metadata', { thread_id: 'thread-1', run_id: 'run-1' }],
        [
          'result',
          {
            ...awaitingSnapshot(),
            validation: { issues: [] },
            interrupt_payload: { ...reviewPayload(), blocking_errors: [], unresolved_errors: [] },
          },
        ],
        ['done', {}],
      ]),
    }),
  )
  await page.route('**/api/v1/runs/*/resume', (route) => {
    bodies.push(route.request().postDataJSON() as Record<string, unknown>)
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: sse([['result', completedSnapshot('', '')], ['done', {}]]),
    })
  })
  await page.route('**/api/v1/threads/*', (route) => route.fulfill({ json: awaitingSnapshot() }))

  await page.goto('/')
  await page.getByLabel('会议原文').fill('会议原文：灰度开关要补文档。')
  await page.getByRole('button', { name: '生成任务' }).click()

  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('#override-form')).toHaveCount(0)
  const approve = page.getByRole('button', { name: '一键批准并完成' })
  await expect(approve).toBeEnabled()
  await approve.click()

  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0].approve).toBe(true)
  expect(bodies[0]).not.toHaveProperty('override_reason')
  expect(bodies[0]).not.toHaveProperty('operator_claim')
})
