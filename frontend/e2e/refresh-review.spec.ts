import { expect, test } from '@playwright/test'

test('审核中刷新后续上，改字段、批准、导出', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '示例3' }).click()
  await expect(page.locator('textarea')).not.toHaveValue('')
  await page.getByRole('button', { name: '生成任务' }).click()
  await expect(page.getByRole('heading', { name: '无法从原文确认主责的任务' })).toBeVisible({
    timeout: 60_000,
  })
  const selects = page.locator('.hitl-assign select')
  const count = await selects.count()
  for (let i = 0; i < count; i += 1) {
    await selects.nth(i).selectOption({ index: 1 })
  }
  await page.getByRole('button', { name: '提交分配' }).click()
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 60_000 })

  await page.reload()
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 60_000 })
  await page.locator('.task-card').first().click()
  await page.getByPlaceholder('YYYY-MM-DD').fill('2026-12-01')
  await page.getByRole('button', { name: '保存字段' }).click()
  await expect(page.getByRole('heading', { name: '审核任务清单' })).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: '一键批准并完成' }).click()
  await page.getByRole('tab', { name: 'Markdown' }).click()
  await expect(page.locator('.md-preview')).toContainText('2026-12-01', { timeout: 60_000 })
})
