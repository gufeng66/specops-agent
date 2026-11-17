import { expect, test, type Page } from '@playwright/test'

async function openDone(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await page.getByRole('button', { name: '示例3' }).click()
  await expect(page.locator('textarea')).not.toHaveValue('')
  await page.getByRole('button', { name: '生成任务' }).click()
  await expect(page.getByRole('heading', { name: '无法从原文确认主责的任务' })).toBeVisible({
    timeout: 60_000,
  })

  const selects = page.locator('.hitl-assign select')
  for (let i = 0; i < await selects.count(); i += 1) {
    await selects.nth(i).selectOption({ index: 1 })
  }
  await page.getByRole('button', { name: '提交分配' }).click()
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: '一键批准并完成' }).click()
  await expect(page.getByRole('heading', { name: '任务已整理完成' })).toBeVisible({ timeout: 60_000 })
}

test('完成页桌面打开原文时显示在结果右侧', async ({ page }) => {
  await openDone(page)

  const toggle = page.getByRole('button', { name: '查看原文' })
  await expect(page.locator('.source-pane')).toBeHidden()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')

  const pageScrollBefore = await page.evaluate(() => window.scrollY)
  await toggle.click()

  await expect(page.locator('.shell.layout-split')).toBeVisible()
  await expect(page.locator('.source-pane')).toBeVisible()
  await expect(toggle).toHaveText('收起原文')
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')

  const layout = await page.evaluate(() => {
    const main = document.querySelector('.stage-main')?.getBoundingClientRect()
    const source = document.querySelector('.source-pane')?.getBoundingClientRect()
    return {
      mainRight: main?.right ?? 0,
      sourceLeft: source?.left ?? 0,
      pageScroll: window.scrollY,
    }
  })
  expect(layout.sourceLeft).toBeGreaterThanOrEqual(layout.mainRight)
  expect(layout.pageScroll).toBe(pageScrollBefore)

  await toggle.click()
  await expect(page.locator('.source-pane')).toBeHidden()
  await expect(page.locator('.shell.layout-stack')).toBeVisible()
})
