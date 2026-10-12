/**
 * 公司研究档案 API 客户端（P0.5）。
 *
 * 对接 server/routes/research_routes.py 暴露的 5 个端点：
 *   POST/GET /api/research/companies/{symbol}/runs
 *   GET      /api/research/companies/{symbol}/latest
 *   GET      /api/research/runs/{run_id}
 *   GET      /api/research/runs/{run_id}/diff?base_run_id=...
 *
 * 严格遵循 P0 边界：
 * - 不暴露内部路径或异常 traceback。
 * - 缺数据返回 200 + degraded=true；仅非法输入才抛错。
 * - 错误统一携带中文 message、稳定 code 与 trace_id。
 */
import service from './index';

/** 统一响应包装。 */
interface ApiResponse<T> {
  ok: boolean;
  data?: T;
  meta?: Record<string, unknown>;
  error?: {
    code?: string;
    message?: string;
    trace_id?: string;
  };
}

/** 单条运行索引（不含大体积 source_snapshots）。 */
export interface ResearchRunRecord {
  run_id: string;
  symbol: string;
  as_of: string;
  schema_version: string;
  status: string;
  created_at: string;
  source_fingerprint: string;
  model_versions: Record<string, unknown>;
}

/** 分页元信息。 */
export interface RunsListMeta {
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  symbol: string;
}

/** 分页列表响应。 */
export interface RunsListResponse {
  items: ResearchRunRecord[];
  meta: RunsListMeta;
}

/** Readiness 单维度。 */
export interface ReadinessDimension {
  status: 'ok' | 'partial' | 'blocked' | 'not_computable' | string;
  score: number | null;
  missing_items: string[];
  evidence_refs: string[];
}

/** 八维 readiness 总览。 */
export type ResearchReadiness = Record<string, ReadinessDimension>;

/** CompanyDossier（按 section 切片）。 */
export interface CompanyDossier {
  symbol: string;
  company_name?: string;
  industry?: string;
  as_of?: string;
  schema_version?: string;
  company_profile?: Record<string, unknown>;
  financial_history?: Array<Record<string, unknown>>;
  financial_quality?: Record<string, unknown>;
  valuation?: Record<string, unknown>;
  valuation_context?: Record<string, unknown>;
  technical_context?: Record<string, unknown>;
  earnings_forecasts?: Array<Record<string, unknown>>;
  announcements?: Array<Record<string, unknown>>;
  industry_context?: Record<string, unknown>;
  profit_bridge?: Record<string, unknown>;
  expectation_context?: Record<string, unknown>;
  research_dimensions?: Record<string, unknown>;
  research_assessment?: Record<string, unknown>;
  source_snapshots?: Record<string, unknown>;
  degradation_reasons?: string[];
  research_readiness?: ResearchReadiness;
  research_facts?: Array<Record<string, unknown>>;
  research_assumptions?: Array<Record<string, unknown>>;
}

/** 运行详情 bundle。 */
export interface RunBundle {
  run: ResearchRunRecord | null;
  dossier: CompanyDossier | null;
  acceptance?: Record<string, unknown> | null;
  degraded?: boolean;
  degradation_reason?: string;
  include?: string[];
  trace_id?: string;
}

/** POST 创建 run 响应。 */
export interface CreateRunResponse {
  run_id: string;
  symbol: string;
  as_of: string;
  status: string;
  degradation_reasons: string[];
  notice: string;
  trace_id: string;
}

/** Diff 结果。 */
export interface RunDiffResponse {
  base_run: ResearchRunRecord | null;
  compare_run: ResearchRunRecord | null;
  facts: {
    verified_edges: { added: unknown[]; removed: unknown[] };
    observed_facts: { added: unknown[]; removed: unknown[] };
    forward_plans: { added: unknown[]; removed: unknown[] };
  };
  assumptions: Record<string, unknown>;
  expectations: Record<string, unknown>;
  valuation: Record<string, unknown>;
  gaps: { added: string[]; removed: string[] };
  degraded: boolean;
  degradation_reason?: string;
  trace_id: string;
}

/** API 抛错携带的元信息。 */
export class CompanyResearchApiError extends Error {
  code: string;
  trace_id?: string;
  status?: number;

  constructor(message: string, code = 'UNKNOWN', trace_id?: string, status?: number) {
    super(message);
    this.name = 'CompanyResearchApiError';
    this.code = code;
    this.trace_id = trace_id;
    this.status = status;
  }
}

/** 统一 GET 包装：识别 ok=false 并抛出携带 code/trace_id 的错误。 */
async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const response = await service.get<ApiResponse<T>>(path, { params });
  if (!response.data?.ok) {
    const err = response.data?.error || {};
    throw new CompanyResearchApiError(
      err.message || '请求失败',
      err.code || 'UNKNOWN',
      err.trace_id,
      response.status,
    );
  }
  return response.data.data as T;
}

/** 统一 POST 包装。 */
async function post<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  const response = await service.post<ApiResponse<T>>(path, body || {});
  if (!response.data?.ok) {
    const err = response.data?.error || {};
    throw new CompanyResearchApiError(
      err.message || '请求失败',
      err.code || 'UNKNOWN',
      err.trace_id,
      response.status,
    );
  }
  return response.data.data as T;
}

/**
 * 创建一次公司研究运行。
 *
 * @param symbol A 股代码（6 位数字 + .SH/.SZ/.BJ）。
 * @param asOf PIT 时点（YYYY-MM-DD）。
 * @param options.lookback_days 回看天数（30-730，默认 120）。
 * @param options.enqueue_gaps 是否将缺口写入 task 队列（默认 false）。
 */
export function createRun(
  symbol: string,
  asOf: string,
  options: { lookback_days?: number; enqueue_gaps?: boolean } = {},
): Promise<CreateRunResponse> {
  return post<CreateRunResponse>(`/api/research/companies/${encodeURIComponent(symbol)}/runs`, {
    as_of: asOf,
    lookback_days: options.lookback_days ?? 120,
    enqueue_gaps: options.enqueue_gaps ?? false,
  });
}

/**
 * 分页列出公司运行索引。
 */
export async function listRuns(
  symbol: string,
  params: { as_of?: string; status?: string; page?: number; page_size?: number } = {},
): Promise<RunsListResponse> {
  // list_runs 在 research_routes.py 直接返回 {data: [...], meta: {...}}
  const response = await service.get<ApiResponse<ResearchRunRecord[]>>(
    `/api/research/companies/${encodeURIComponent(symbol)}/runs`,
    { params },
  );
  if (!response.data?.ok) {
    const err = response.data?.error || {};
    throw new CompanyResearchApiError(
      err.message || '请求失败',
      err.code || 'UNKNOWN',
      err.trace_id,
      response.status,
    );
  }
  const items = (response.data.data || []) as ResearchRunRecord[];
  const meta = (response.data.meta || {}) as RunsListMeta;
  return { items, meta };
}

/**
 * 读取公司最新研究运行；不存在时返回 degraded=true（不抛错）。
 */
export function getLatest(symbol: string, include?: string[]): Promise<RunBundle> {
  const params = include && include.length ? { include: include.join(',') } : undefined;
  return get<RunBundle>(`/api/research/companies/${encodeURIComponent(symbol)}/latest`, params);
}

/**
 * 读取单个运行详情，可按 section 懒加载。
 */
export function getRun(runId: string, include?: string[]): Promise<RunBundle> {
  const params = include && include.length ? { include: include.join(',') } : undefined;
  return get<RunBundle>(`/api/research/runs/${encodeURIComponent(runId)}`, params);
}

/**
 * 比较两个 run 的事实、假设、预期、估值、缺口变化。
 */
export function diffRuns(runId: string, baseRunId: string): Promise<RunDiffResponse> {
  return get<RunDiffResponse>(
    `/api/research/runs/${encodeURIComponent(runId)}/diff`,
    { base_run_id: baseRunId },
  );
}
