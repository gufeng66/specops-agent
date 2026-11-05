import { expect, test, type Page } from '@playwright/test'

async function openAssign(page: Page, sample = '示例3') {
  await page.goto('/')
  await page.getByRole('button', { name: sample }).click()
  await expect(page.locator('textarea')).not.toHaveValue('')
  await page.getByRole('button', { name: '生成任务' }).click()
  await expect(page.getByRole('heading', { name: '无法从原文确认主责的任务' })).toBeVisible({
    timeout: 60_000,
  })
}

/** 只读正文的底边不能越过字数和工具栏，否则长原文会把按钮盖掉。 */
async function expectNoOverlap(page: Page) {
  const box = await page.evaluate(() => {
    const rect = (selector: string) => {
      const el = document.querySelector(selector)
      return el ? el.getBoundingClientRect() : null
    }
    const read = rect('.source-read')
    const charCount = rect('.char-count')
    const toolbar = rect('.source-toolbar')
    const scroller = document.querySelector('.source-read') as HTMLElement
    return {
      readBottom: read?.bottom ?? 0,
      charCountTop: charCount?.top ?? 0,
      toolbarTop: toolbar?.top ?? 0,
      overflowY: scroller ? getComputedStyle(scroller).overflowY : '',
      // 长原文必须能在正文内部滚动，才能保证上面的位置关系成立
      canScroll: scroller ? scroller.scrollHeight > scroller.clientHeight : false,
    }
  })
  expect(box.overflowY).toBe('auto')
  expect(box.canScroll).toBe(true)
  expect(box.readBottom).toBeLessThanOrEqual(box.charCountTop)
  expect(box.readBottom).toBeLessThanOrEqual(box.toolbarTop)
}

test('长原文定位后只滚动正文内部，不覆盖字数和工具栏', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await openAssign(page)
  expect(await page.locator('.char-count').innerText()).toContain('2908 字')

  const pageScrollBefore = await page.evaluate(() => scrollY)
  await page.locator('.hitl-assign .locate-link').first().click()
  await page.waitForTimeout(400)

  // 高亮可见，并且落在正文可视区域内
  const mark = page.locator('.source-read mark')
  await expect(mark).toHaveCount(1)
  await expect(mark).toBeVisible()
  const inside = await page.evaluate(() => {
    const markEl = document.querySelector('.source-read mark')!.getBoundingClientRect()
    const box = document.querySelector('.source-read')!.getBoundingClientRect()
    return markEl.top >= box.top && markEl.bottom <= box.bottom
  })
  expect(inside).toBe(true)

  // 定位不能把整页滚走
  expect(await page.evaluate(() => scrollY)).toBe(pageScrollBefore)

  await expectNoOverlap(page)
})

test('页面已滚动时定位也不覆盖工具栏', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await openAssign(page)
  await page.mouse.wheel(0, 400)
  await page.waitForTimeout(200)

  await page.locator('.hitl-assign .locate-link').first().click()
  await page.waitForTimeout(400)

  await expect(page.locator('.source-read mark')).toHaveCount(1)
  await expectNoOverlap(page)
})

test('放大原文后仍不覆盖工具栏，高亮保持可见', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await openAssign(page)
  await page.locator('.hitl-assign .locate-link').first().click()
  await page.waitForTimeout(400)

  await page.getByRole('button', { name: '放大原文' }).click()
  await page.waitForTimeout(400)

  await expect(page.locator('[role="dialog"]')).toBeVisible()
  await expect(page.locator('.source-read mark')).toBeVisible()
  // 放大后容器尺寸变了，要重新居中，不能停在旧滚动位置
  const visible = await page.evaluate(() => {
    const markEl = document.querySelector('.source-read mark')!.getBoundingClientRect()
    const box = document.querySelector('.source-read')!.getBoundingClientRect()
    return markEl.top >= box.top && markEl.bottom <= box.bottom
  })
  expect(visible).toBe(true)
  await expectNoOverlap(page)
})
