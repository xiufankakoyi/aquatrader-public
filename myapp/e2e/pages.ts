/**
 * 页面声明注册表
 *
 * 集中维护"任务要求路径 -> 实际 vue-router 路径 -> 业务标识"的映射,
 * spec 与 README 共享同一份事实, 避免双重维护。
 *
 * 路径差异原因:
 *  - 项目 vue-router (src/router/index.ts) 实际路径与任务文案略有出入
 *  - 这里把任务中列出的 9 个页面全部纳入, 用 'taskPath' 标注原始文案
 *  - 'routePath' 才是真实可访问的 URL
 */
export interface PageSpec {
  /** 任务原文中的路径 (用作测试名 & 报告键) */
  taskPath: string;
  /** 实际 vue-router 路径 (真实可访问) */
  routePath: string;
  /** 业务中文名 */
  title: string;
  /** 是否预期会发起业务 API 请求 (用于过滤 pageErrors 中的第三方噪声) */
  expectsApi: boolean;
}

/**
 * 9 个主要页面声明
 * 顺序与 T1.4 任务要求保持一致, 方便报告输出
 */
export const PAGES: PageSpec[] = [
  { taskPath: '/',                  routePath: '/dashboard',      title: '总览看板 Dashboard',       expectsApi: true },
  { taskPath: '/dragon-eye',        routePath: '/dragon-eye',     title: 'DragonEye 龙虎榜',         expectsApi: true },
  { taskPath: '/strategy-center',   routePath: '/strategy-center', title: '策略中心',                expectsApi: true },
  { taskPath: '/stock-screener',    routePath: '/stock-screener', title: '股票量化筛选器',            expectsApi: true },
  { taskPath: '/history-records',   routePath: '/history',        title: '历史记录',                  expectsApi: true },
  { taskPath: '/similarity',        routePath: '/similarity',     title: 'K线形态研究',                expectsApi: true },
  { taskPath: '/portfolio-analysis', routePath: '/portfolio',      title: '实盘持仓分析',              expectsApi: true },
  { taskPath: '/stock-sentiment',   routePath: '/stock-sentiment', title: '股票风评',                  expectsApi: true },
  { taskPath: '/industry-chain',    routePath: '/industry-chain', title: '产业链雷达 (题材支持网)',   expectsApi: true },
];

/**
 * 失败时打印的格式化辅助
 *
 * 输出: "endpoint=X, status=Y, console=Z" 形式, 便于 grep
 */
export function fmtFailureContext(input: {
  endpoint?: string;
  status?: number;
  consoleError?: string;
  pageError?: string;
}): string {
  const parts: string[] = [];
  if (input.endpoint) parts.push(`endpoint=${input.endpoint}`);
  if (input.status !== undefined) parts.push(`status=${input.status}`);
  if (input.consoleError) parts.push(`console=${truncate(input.consoleError, 160)}`);
  if (input.pageError) parts.push(`pageerror=${truncate(input.pageError, 160)}`);
  return parts.join(' | ');
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}...` : s;
}
