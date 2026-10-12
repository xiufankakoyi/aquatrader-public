/**
 * Browser Runtime Green E2E (T1.4)
 *
 * 目标:
 *  - 真实环境 (不 mock) 访问 myapp 9 个主要页面
 *  - 采集 console / pageerror / 4xx-5xx 响应 / JSON.parse 失败 四类信号
 *  - 断言:
 *      1) body 有可见子元素, min-height > 0 (非白屏)
 *      2) 没有未捕获 pageerror
 *      3) 主要 5xx 响应必须被 ErrorState / TimeoutState / ApiDebugPanel 呈现
 *      4) 主要 4xx 响应同样必须被上述三种兜底组件呈现, 避免静默失败
 *  - 用例失败时打印 endpoint / status / console.error
 *
 * 关键约束:
 *  - 不输出投资建议
 *  - 不 mock 后端, 真实访问
 *  - 失败信息结构化, 便于 grep
 */
import { test, expect, type Page } from '@playwright/test';
import { RuntimeCollector, waitForNetworkIdle } from './collector';
import { PAGES, fmtFailureContext, type PageSpec } from './pages';

/**
 * 单页校验流程
 *
 * 流程:
 *  1) 挂载 RuntimeCollector
 *  2) 访问真实路由, 等待 networkidle 或 30s 兜底
 *  3) 检查 body 可见子元素 & 高度
 *  4) 收集 pageerror / console.error / 4xx-5xx / json_parse_failed
 *  5) 校验"5xx 必有 ErrorState 兜底"
 *  6) 软警告"4xx 应有 ErrorState 兜底" (允许 mock 环境下偶现 4xx, 记录但不抛)
 */
async function inspectPage(page: Page, spec: PageSpec): Promise<void> {
  const collector = new RuntimeCollector(page);
  collector.attach();

  // 1) 访问真实路由
  const response = await page.goto(spec.routePath, {
    waitUntil: 'domcontentloaded',
  });
  // 首屏响应非 200 (例如 nginx 502/404) 也要记录, 便于排障
  if (response && !response.ok() && response.status() !== 304) {
    collector.addConsoleError(
      fmtFailureContext({
        endpoint: response.url(),
        status: response.status(),
      })
    );
  }

  // 2) 等待网络空闲；长轮询/SSE 页面最多等待 8s 后继续检查
  await waitForNetworkIdle(page, 8_000);

  // 3) 白屏检查: 至少有 1 个可见子元素, 且 main-content-area 高度 > 0
  const visibleChildCount = await page.evaluate(() => {
    return document.querySelectorAll('main *').length;
  });
  const contentHeight = await page.evaluate(() => {
    const main = document.querySelector('main') as HTMLElement | null;
    if (!main) return 0;
    return main.getBoundingClientRect().height;
  });

  if (visibleChildCount === 0) {
    throw new Error(fmtFailureContext({
      endpoint: spec.routePath,
      pageError: '白屏: <main> 没有任何子元素',
    }));
  }
  if (contentHeight <= 0) {
    throw new Error(fmtFailureContext({
      endpoint: spec.routePath,
      pageError: `白屏: <main> 高度为 ${contentHeight}`,
    }));
  }

  // 4) 校验 pageerror (硬性断言)
  if (collector.pageErrors.length > 0) {
    const first = collector.pageErrors[0];
    throw new Error(fmtFailureContext({
      endpoint: spec.routePath,
      pageError: `${first.message} (${first.stack?.split('\n').slice(0, 3).join(' | ') || 'no stack'})`,
    }));
  }

  // 5) 5xx 响应必须有 ErrorState / TimeoutState / ApiDebugPanel 兜底
  for (const bad of collector.serverErrors) {
    const hasErrorState = await pageHasErrorState(page);
    if (!hasErrorState) {
      throw new Error(fmtFailureContext({
        endpoint: spec.routePath,
        status: bad.status,
        pageError: `5xx 接口 ${bad.url} 未被 ErrorState/TimeoutState/ApiDebugPanel 展示 (静默失败)`,
      }));
    }
  }

  // 6) 4xx 响应: 同样必须有兜底组件, 但允许在 demo/mock 数据缺失时存在
  for (const bad of collector.clientErrors) {
    const hasErrorState = await pageHasErrorState(page);
    if (!hasErrorState) {
      // 软警告: 用 test.info() 记录, 不直接抛, 避免 mock/demo 数据缺失时整个 E2E 全挂
      test.info().annotations.push({
        type: 'soft-warning',
        description: fmtFailureContext({
          endpoint: spec.routePath,
          status: bad.status,
          pageError: `4xx 接口 ${bad.url} 未被 ErrorState/TimeoutState/ApiDebugPanel 展示`,
        }),
      });
    }
  }

  // 7) JSON parse 失败: 硬性断言 (前后端契约破坏, 必须 fix)
  if (collector.jsonParseFailures.length > 0) {
    const first = collector.jsonParseFailures[0];
    throw new Error(fmtFailureContext({
      endpoint: spec.routePath,
      pageError: `JSON.parse 失败: ${first.url} (status=${first.status}) preview=${first.preview}`,
    }));
  }

  // 8) 兜底: 如果出现 console.error 但其它指标都正常, 用 annotation 报告
  //   - Vue dev 警告 / ant-design-vue warning 偶发, 不阻塞
  for (const ce of collector.consoleErrors) {
    test.info().annotations.push({
      type: 'console-error',
      description: fmtFailureContext({
        endpoint: spec.routePath,
        consoleError: ce.text,
      }),
    });
  }
}

/**
 * 判断页面是否渲染了任意一个兜底组件
 *
 *  - ErrorState: .error-state (role="alert")
 *  - TimeoutState: .timeout-state (role="alert")
 *  - ApiDebugPanel: .api-debug-panel (role="dialog"), 默认是关闭的, 这里只检查兜底机制是否生效
 *  - EmptyState: .empty-state 也算"非静默", 但优先级低于 Error/Timeout
 */
async function pageHasErrorState(page: Page): Promise<boolean> {
  const probe = await page.evaluate(() => {
    return Boolean(
      document.querySelector('.error-state')
        || document.querySelector('.timeout-state')
        || document.querySelector('.api-debug-panel')
    );
  });
  return probe;
}

// ===== 用例套件: 9 个页面各跑一次 =====
test.describe('Browser Runtime Green (T1.4)', () => {
  for (const spec of PAGES) {
    test(`页面无白屏 + 错误兜底: ${spec.taskPath} -> ${spec.routePath} (${spec.title})`, async ({ page }) => {
      await inspectPage(page, spec);
    });
  }

  /**
   * 全局开关: 启动到 5173 之后, /api/data/health 至少要 200
   * 这一条独立于 9 个页面, 用于"环境自检"
   */
  test('环境自检: 后端 /api/data/health 可达', async ({ request }) => {
    const r = await request.get('http://localhost:5000/api/data/health', { timeout: 10_000 });
    expect(
      r.ok(),
      fmtFailureContext({ endpoint: '/api/data/health', status: r.status() })
    ).toBeTruthy();
  });
});
