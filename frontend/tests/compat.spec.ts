import { expect, test } from '@playwright/test'

test('兼容转换面板：生成批次、两版视图、待核对区、字段版本依赖', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/schema_evolve')
  await expect(page.getByTestId('compat-panel')).toBeVisible()
  await expect(page.getByTestId('compat-change')).toHaveValue('2')
  await page.getByTestId('compat-boundary').fill('500')
  await page.getByTestId('compat-backfill').check()
  await page.getByTestId('compat-run').click()
  await expect(page.getByTestId('compat-message')).toContainText('写入 8 行', { timeout: 10000 })

  const panel = page.locator('[data-testid^="compat-detail-panel-"]')
  await expect(panel).toBeVisible()
  await expect(page.getByTestId('compat-dep-v1')).toContainText('读 金额')
  await expect(page.getByTestId('compat-dep-v2')).toContainText('读 实付金额')
  await expect(page.getByTestId('compat-review-ORD-104')).toContainText('待核对')
  await expect(page.getByTestId('compat-review-ORD-108')).toContainText('待核对')
  await expect(page.getByTestId('compat-check-重复写入')).toContainText('通过')
  await expect(panel).toContainText('ORD-101')
  await expect(panel).toContainText('ORD-110')

  await page.locator('[data-testid^="compat-rollback-"]').first().click()
  await expect(page.getByTestId('compat-rollback-panel')).toContainText('回滚后读取字段：金额')

  expect(errors).toEqual([])
})
