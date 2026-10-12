/**
 * 移动端 API 服务
 * 提供所有移动端相关的 API 调用，包括认证、日报、收件箱、观察池、任务、报告和命令
 */
import axios from './index';

const BASE = '/api/mobile';

// ==================== 请求拦截器：自动注入移动端 token ====================

axios.interceptors.request.use((config) => {
  const url = config.url || '';
  // 只为需要认证的移动端接口自动注入 token，避免污染登录/提示接口
  if (url.startsWith('/api/mobile/') && !url.startsWith('/api/mobile/auth/login') && !url.startsWith('/api/mobile/auth/pin')) {
    const token = localStorage.getItem('mobile_auth_token');
    if (token && !config.headers?.Authorization) {
      config.headers = config.headers || {};
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

// ==================== 类型定义 ====================

/** 移动端首页摘要 */
export interface MobileSummary {
  date: string;
  latest_daily_brief: string | null;
  top_candidates: any[];
  inbox_stats: Record<string, number>;
  watchlist_stats: Record<string, number>;
  task_stats: Record<string, number>;
  last_updated: string;
}

/** 日报内容 */
export interface DailyBrief {
  daily_brief: { markdown: string; html: string } | null;
  perilla_discovery: { markdown: string; html: string } | null;
}

/** 收件箱条目 */
export interface InboxItem {
  item_id: string;
  company_name: string;
  symbol: string;
  chain_node: string;
  concept: string;
  evidence_level: string;
  classification: string;
  total_score: number;
  inbox_status: string;
  research_priority_score: number;
  research_priority_level: string;
  bottleneck_logic: string;
}

/** 观察池条目 */
export interface WatchlistItem extends InboxItem {}

/** 任务信息 */
export interface TaskInfo {
  task_id: string;
  task_type: string;
  status: string;
  params: Record<string, any>;
  source: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_ms: number | null;
  log_path: string | null;
  report_paths: string[];
  error: Record<string, any> | null;
  progress: Record<string, any>;
}

/** 报告元信息 */
export interface ReportInfo {
  report_id: string;
  filename: string;
  size: number;
  modified_at: string;
}

/** 报告内容 */
export interface ReportContent {
  report_id: string;
  filename: string;
  content: string;
  html: string;
  size: number;
  modified_at: string;
}

/** 命令执行结果 */
export interface CommandResult {
  accepted: boolean;
  task_id: string | null;
  requires_confirmation: boolean;
  confirmation_token: string | null;
  message: string;
  error: Record<string, any> | null;
}

// ==================== 认证 API ====================

/** 使用 Token 登录 */
export async function loginWithToken(token: string): Promise<any> {
  const res = await axios.post('/api/mobile/auth/login', { token });
  return res.data;
}

/** 使用 PIN 登录 */
export async function loginWithPin(pin: string): Promise<any> {
  const res = await axios.post('/api/mobile/auth/login', { pin });
  return res.data;
}

/** 登出 */
export async function logout(): Promise<any> {
  const res = await axios.post('/api/mobile/auth/logout');
  return res.data;
}

/** 获取认证状态 */
export async function getAuthStatus(): Promise<any> {
  const res = await axios.get('/api/mobile/auth/status');
  return res.data;
}

// ==================== 移动端数据 API ====================

/** 获取首页摘要 */
export async function getSummary(): Promise<any> {
  const res = await axios.get(`${BASE}/summary`);
  return res.data;
}

/** 获取日报内容 */
export async function getDaily(): Promise<any> {
  const res = await axios.get(`${BASE}/daily`);
  return res.data;
}

/** 获取收件箱列表 */
export async function getInbox(params?: {
  status?: string;
  classification?: string;
  limit?: number;
  offset?: number;
}): Promise<any> {
  const res = await axios.get(`${BASE}/inbox`, { params });
  return res.data;
}

/** 获取基于收件箱真实数据的产业链拓扑图 */
export async function getMobileChain(concept?: string): Promise<any> {
  const params = concept ? { concept } : {};
  const res = await axios.get(`${BASE}/chain`, { params });
  return res.data;
}

/** 列出收件箱中可用于产业链展示的所有真实概念 */
export async function getMobileChainConcepts(): Promise<any> {
  const res = await axios.get(`${BASE}/chain/concepts`);
  return res.data;
}

/** 获取观察池列表 */
export async function getWatchlist(params?: {
  classification?: string;
  min_priority?: number;
  limit?: number;
  offset?: number;
}): Promise<any> {
  const res = await axios.get(`${BASE}/watchlist`, { params });
  return res.data;
}

/** 获取任务列表 */
export async function getTasks(params?: {
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<any> {
  const res = await axios.get(`${BASE}/tasks`, { params });
  return res.data;
}

/** 获取单个任务详情 */
export async function getTask(taskId: string): Promise<any> {
  const res = await axios.get(`${BASE}/tasks/${taskId}`);
  return res.data;
}

/** 获取报告列表 */
export async function getReports(): Promise<any> {
  const res = await axios.get(`${BASE}/reports`);
  return res.data;
}

/** 获取单个报告内容 */
export async function getReport(reportId: string): Promise<any> {
  const res = await axios.get(`${BASE}/reports/${reportId}`);
  return res.data;
}

/** 提交命令 */
export async function submitCommand(
  command: string,
  params: Record<string, any> = {},
  confirm: boolean = false
): Promise<any> {
  const res = await axios.post(`${BASE}/command`, { command, params, confirm });
  return res.data;
}

/** 确认命令执行 */
export async function confirmCommand(confirmationToken: string): Promise<any> {
  const res = await axios.post(`${BASE}/command/confirm`, {
    confirmation_token: confirmationToken,
    source: 'mobile_web',
    user_id: 'mobile_user'
  });
  return res.data;
}
