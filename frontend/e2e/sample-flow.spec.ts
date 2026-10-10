import { expect, test } from '@playwright/test'

test('示例3：分析、分配、审核、导出', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '示例3' }).click()
  await expect(page.locator('textarea')).not.toHaveValue('')
  await page.getByRole('button', { name: '开始分析' }).click()
  await expect(page.getByRole('heading', { name: '无法从原文确认主责的任务' })).toBeVisible({
    timeout: 60_000,
  })

  const selects = page.locator('.hitl-assign select')
  await expect(selects.first()).toBeVisible()
  const count = await selects.count()
  for (let i = 0; i < count; i += 1) {
    await selects.nth(i).selectOption({ index: 1 })
  }
  await page.getByRole('button', { name: '提交分配' }).click()
  await expect(page.getByText('审核任务清单')).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: '一键批准并完成' }).click()
  await expect(page.locator('.md-preview')).toContainText('任务', { timeout: 60_000 })
})
