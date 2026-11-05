import { expect, test, type Page } from '@playwright/test'

/** 示例3 的代词任务会停在分配面板，正好用来验证原文定位。 */
async function openAssign(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: '示例3' }).click()
  await expect(page.locator('textarea')).not.toHaveValue('')
  await page.getByRole('button', { name: '生成任务' }).click()
  await expect(page.getByRole('heading', { name: '无法从原文确认主责的任务' })).toBeVisible({
    timeout: 60_000,
  })
}

test('分配面板点「点击这里定位原文」会标出该任务的依据', async ({ page }) => {
  await openAssign(page)
  const link = page.locator('.hitl-assign .locate-link').first()
  await expect(link).toBeVisible()
  await expect(page.locator('.source-read mark')).toHaveCount(0)

  await link.click()

  const mark = page.locator('.source-read mark')
  await expect(mark).toHaveCount(1)
  await expect(mark).toContainText('她')
})

test('窄屏下点「点击这里定位原文」会打开原文抽屉并标出依据', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  await openAssign(page)
  // 窄屏抽屉未打开时整个面板是隐藏的，只标 <mark> 用户看不到，所以必须一起打开。
  await expect(page.locator('.source-pane')).toBeHidden()

  await page.locator('.hitl-assign .locate-link').first().click()

  await expect(page.locator('.source-pane')).toBeVisible()
  await expect(page.locator('.source-read mark')).toBeVisible()
})
