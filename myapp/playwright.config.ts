/**
 * Playwright E2E 浏览器运行时配置
 *
 * 目标:
 *  - 覆盖 myapp 9 个主要页面在真实运行时的白屏/错误/接口 4xx-5xx 行为
 *  - 同时拉起前端 (Vite, 5173) 与后端 (granian, 5000), 禁止 mock
 *  - 失败信息包含 endpoint / status / console.error, 便于排障
 *
 * 关键约束:
 *  - baseURL 固定为 5173, 与 vite.config.ts 的 strictPort 保持一致
 *  - webServer 命令分别启动后端 (5000) 与前端 (5173), 串行等待 ready
 *  - 仅启用 chromium, 减少 CI 资源占用
 *  - 报告器 list: 控制台逐用例打印, 便于本地调试
 */
import { defineConfig, devices } from '@playwright/test';

// 后端日志文件位置 (相对项目根), 避免与前端日志混在一起
const BACKEND_LOG = './logs/e2e-backend.log';
const FRONTEND_LOG = './logs/e2e-frontend.log';

export default defineConfig({
  // 测试根目录: myapp/e2e, 便于在 myapp 目录下直接 npm run e2e
  testDir: './e2e',

  // 全局默认 30s 超时 (含单步 expect 与 beforeEach)
  timeout: 30_000,

  // 跨用例复用 context 避免副作用累积, 但禁用 parallel 减少端口/资源冲突
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,

  // 报告器: list 控制台逐行, 失败时打印 endpoint/status/console
  reporter: [
    ['list'],
    ['html', { outputFolder: '../data/reports/playwright-report', open: 'never' }],
    ['json', { outputFile: '../data/reports/playwright-results.json' }],
  ],
  outputDir: '../data/reports/playwright-artifacts',

  use: {
    // 前端 dev server 默认 5173, 与 vite.config.ts 保持一致
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // 单页接口拉取窗口默认 15s, 留出 30s 给首屏渲染与首屏数据
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    // 忽略 HTTPS 错误 (本地 dev)
    ignoreHTTPSErrors: true,
  },

  // 浏览器矩阵: 仅 chromium
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // 启动 backend (port 5000) + frontend (port 5173)
  //  - 不在 mock 环境运行, 真实访问 /api/*
  //  - reuseExistingServer=true 允许复用开发者本机已启动的服务
  //  - 串行启动: backend 先 ready, 前端再启动, 减少代理探测到 502 的概率
  webServer: [
    {
      command: 'python -X utf8 sandbox/run_playwright_server.py',
      cwd: '..',
      port: 5000,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      cwd: '.',
      port: 5173,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
});
