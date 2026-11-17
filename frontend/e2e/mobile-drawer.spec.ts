import { expect, test, type Page } from '@playwright/test'

/** 示例3 会停在分配面板，窄屏下正好用来看原文抽屉。 */
async function openAssign(page: Page) {
  await page.setViewportSize({ width: 900, height: 800 })
  await page.goto('/')
  await page.getByRole('button', { name: '示例3' }).click()
  await expect(page.locator('textarea')).not.toHaveValue('')
  await page.getByRole('button', { name: '生成任务' }).click()
  await expect(page.getByRole('heading', { name: '无法从原文确认主责的任务' })).toBeVisible({
    timeout: 60_000,
  })
}

const bodyOverflow = (page: Page) => page.evaluate(() => document.body.style.overflow)

/** 抽屉是弹层：整页滚动被锁，背景 inert，抽屉自身不在 #app 里所以没被一起锁死。 */
async function expectDrawerOpen(page: Page) {
  await expect(page.locator('.source-pane')).toBeVisible()
  await expect.poll(() => bodyOverflow(page)).toBe('hidden')
  const state = await page.evaluate(() => {
    const app = document.getElementById('app') as HTMLElement
    const el = document.querySelector('.source-pane') as HTMLElement
    return { inert: Boolean(app?.inert), inApp: Boolean(app && el && app.contains(el)) }
  })
  expect(state.inert).toBe(true)
  expect(state.inApp).toBe(false)
}

async function expectDrawerClosed(page: Page) {
  await expect(page.locator('.source-pane')).toBeHidden()
  await expect.poll(() => bodyOverflow(page)).toBe('')
  expect(await page.evaluate(() => Boolean(document.getElementById('app')?.inert))).toBe(false)
}

test('窄屏抽屉锁住背景，关闭后解锁并把焦点还回去', async ({ page }) => {
  await openAssign(page)
  expect(await bodyOverflow(page)).toBe('')

  const trigger = page.getByRole('button', { name: '查看原文' })
  await trigger.click()
  await expectDrawerOpen(page)

  // 抽屉里的按钮必须真的能点：若被背景的 inert 波及，这里会超时。
  await page.getByRole('button', { name: '关闭原文' }).click()
  await expectDrawerClosed(page)
  await expect(trigger).toBeFocused()

  // 解锁后再打开一次，确认状态没有残留。
  await trigger.click()
  await expectDrawerOpen(page)
})

test('Esc 关闭抽屉后页面恢复可滚', async ({ page }) => {
  await openAssign(page)
  await page.getByRole('button', { name: '查看原文' }).click()
  await expectDrawerOpen(page)

  await page.keyboard.press('Escape')
  await expectDrawerClosed(page)
})

/** 一路走到完成页，用来验证「只有等待人工操作时才锁滚动」。 */
async function openDone(page: Page) {
  await openAssign(page)
  const selects = page.locator('.hitl-assign select')
  const count = await selects.count()
  for (let i = 0; i < count; i += 1) {
    await selects.nth(i).selectOption({ index: 1 })
  }
  await page.getByRole('button', { name: '提交分配' }).click()
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: '一键批准并完成' }).click()
  await expect(page.getByRole('heading', { name: '任务已整理完成' })).toBeVisible({ timeout: 60_000 })
}

test('完成页窄屏重新打开原文不锁整页滚动', async ({ page }) => {
  await openDone(page)
  expect(await bodyOverflow(page)).toBe('')

  await page.getByRole('button', { name: '查看原文' }).click()
  await expect(page.locator('.source-pane')).toBeVisible()
  // 完成页不再等人工操作：抽屉可以看原文，但不该把整页滚动钉住。
  expect(await bodyOverflow(page)).not.toBe('hidden')

  // 抽屉打开时页面本身仍可滚动（浏览器允许滚动的判据就是 body 没被设成 hidden）。
  const scrollable = await page.evaluate(() => {
    const before = window.scrollY
    window.scrollTo(0, document.body.scrollHeight)
    const moved = window.scrollY !== before || document.body.scrollHeight <= window.innerHeight
    window.scrollTo(0, before)
    return moved
  })
  expect(scrollable).toBe(true)

  await page.getByRole('button', { name: '关闭原文' }).click()
  await expect(page.locator('.source-pane')).toBeHidden()
  await expect.poll(() => bodyOverflow(page)).toBe('')
})

test('提交分配进入下一阶段后抽屉与滚动锁一起退出', async ({ page }) => {
  await openAssign(page)
  await page.getByRole('button', { name: '查看原文' }).click()
  await expectDrawerOpen(page)

  // 背景此时是 inert 的，必须先关抽屉才能操作分配控件。
  await page.getByRole('button', { name: '关闭原文' }).click()
  await expectDrawerClosed(page)

  const selects = page.locator('.hitl-assign select')
  const count = await selects.count()
  for (let i = 0; i < count; i += 1) {
    await selects.nth(i).selectOption({ index: 1 })
  }
  await page.getByRole('button', { name: '提交分配' }).click()

  // 进入下一阶段后滚动锁必须已经放开，抽屉也不该自己弹出来。
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 60_000 })
  await expectDrawerClosed(page)

  // 审核阶段仍能重新打开，说明没有残留的 inert。
  await page.getByRole('button', { name: '查看原文' }).click()
  await expectDrawerOpen(page)
})
