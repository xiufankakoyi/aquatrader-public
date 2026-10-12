import { expect, test } from '@playwright/test';

test('公司研究页加载 v8 正式事实并可切换历史运行', async ({ page }) => {
  await page.goto('/research/company', { waitUntil: 'domcontentloaded' });
  await expect(page.getByText('company_dossier_v8', { exact: true })).toBeVisible();
  await expect(page.locator('.facts-col').first().locator('.col-count')).not.toHaveText('0');

  const runMeta = page.locator('.meta-item').filter({ hasText: '运行 ID' }).locator('.meta-value');
  const currentSuffix = (await runMeta.textContent())?.trim() || '';
  const history = page.locator('.card-history');
  await history.getByRole('button', { name: '加载历史', exact: true }).click();
  await expect(history.locator('tbody tr')).not.toHaveCount(0);

  await history.locator('.ant-select').nth(1).click();
  const options = page.locator('.ant-select-item-option');
  const labels = await options.allTextContents();
  const olderIndex = labels.findIndex((label) => !label.includes(currentSuffix));
  expect(olderIndex).toBeGreaterThanOrEqual(0);
  await options.nth(olderIndex).click();
  await expect(runMeta).not.toHaveText(currentSuffix);
});

test('公司研究页在 375px 宽度无横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/research/company', { waitUntil: 'domcontentloaded' });
  await expect(page.getByText('company_dossier_v8', { exact: true })).toBeVisible();
  const hasOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(hasOverflow).toBe(false);
});

test('公司研究页将 API 400 展示为中文错误摘要', async ({ page }) => {
  await page.goto('/research/company', { waitUntil: 'domcontentloaded' });

  const symbolInput = page.locator('.header-actions input').first();
  await symbolInput.fill('999999.XX');
  await page.getByRole('button', { name: '查询', exact: true }).click();

  const errorBanner = page.locator('.error-banner');
  await expect(errorBanner).toBeVisible();
  await expect(errorBanner).toContainText('symbol 格式非法');
  await expect(errorBanner).toContainText('INVALID_SYMBOL');
  await expect(errorBanner).not.toContainText('Request failed');
});
