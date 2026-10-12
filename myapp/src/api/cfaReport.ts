/**
 * P1.9 CFA 风格研究报告 API 客户端。
 *
 * 对接 server/routes/reporting_routes.py 暴露的 4 个端点：
 *   POST /api/research/reports/cfa                构建 CFA 报告
 *   GET  /api/research/reports/cfa                查询报告列表
 *   GET  /api/research/reports/cfa/<report_id>    单个报告详情
 *   GET  /api/research/reports/cfa/latest         按 PIT 取最新报告
 *
 * 严格遵循 P1.9 边界：
 * - 固定 notice「研究数据产物，不包含交易动作建议。」
 * - 缺数据返回 200 + degraded=true；仅非法输入才抛错。
 * - 错误统一携带中文 message、稳定 code 与 trace_id。
 */
import service from './index';

/** 统一响应包装。 */
interface ApiResponse<T> {
  ok: boolean;
  data?: T;
  notice?: string;
  trace_id?: string;
  degraded?: boolean;
  degradation_reason?: string;
  total?: number;
  error?: {
    code?: string;
    message?: string;
    trace_id?: string;
  };
}

/** 报告章节状态。 */
export type SectionStatus = 'ok' | 'partial' | 'missing' | 'not_applicable';

/** 报告整体状态。 */
export type OverallStatus = 'ok' | 'partial' | 'degraded';

/** 单章节内容。 */
export interface CfaReportSection {
  /** 章节标识（ReportSection.value）。 */
  section: string;
  /** 中文标题。 */
  title: string;
  /** 结构化内容（任意 dict）。 */
  content: Record<string, unknown>;
  /** 章节限制说明列表。 */
  limitations: string[];
  /** 章节来源引用列表。 */
  source_refs: string[];
  /** 章节状态。 */
  section_status: SectionStatus;
}

/** CFA 完整研究报告。 */
export interface CfaReport {
  report_id: string;
  company_symbol: string;
  company_name: string;
  as_of: string;
  lookback_days: number;
  sections: CfaReportSection[];
  overall_status: OverallStatus;
  degradation_reasons: string[];
  notice: string;
  model_version: string;
  created_at: string;
}

/** 列表查询参数。 */
export interface ListCfaReportsParams {
  company_symbol?: string;
  as_of?: string;
  status?: string;
  limit?: number;
}

/** 构建请求参数。 */
export interface BuildCfaReportParams {
  company_symbol: string;
  as_of: string;
  lookback_days?: number;
  save?: boolean;
}

/** API 抛错携带的元信息。 */
export class CfaReportApiError extends Error {
  code: string;
  trace_id?: string;
  status?: number;

  constructor(message: string, code = 'UNKNOWN', trace_id?: string, status?: number) {
    super(message);
    this.name = 'CfaReportApiError';
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
    throw new CfaReportApiError(
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
    throw new CfaReportApiError(
      err.message || '请求失败',
      err.code || 'UNKNOWN',
      err.trace_id,
      response.status,
    );
  }
  return response.data.data as T;
}

/** 构建一份 CFA 报告。 */
export function buildCfaReport(params: BuildCfaReportParams): Promise<CfaReport> {
  return post<CfaReport>('/api/research/reports/cfa', {
    company_symbol: params.company_symbol,
    as_of: params.as_of,
    lookback_days: params.lookback_days ?? 120,
    save: params.save ?? true,
  });
}

/** 列表查询 CFA 报告。 */
export function listCfaReports(p: ListCfaReportsParams = {}): Promise<CfaReport[]> {
  return get<CfaReport[]>('/api/research/reports/cfa', {
    company_symbol: p.company_symbol || '',
    as_of: p.as_of || '',
    status: p.status || '',
    limit: p.limit ?? 100,
  });
}

/** 按 report_id 获取 CFA 报告详情。 */
export function getCfaReport(reportId: string): Promise<CfaReport> {
  return get<CfaReport>(`/api/research/reports/cfa/${encodeURIComponent(reportId)}`);
}

/** 按 PIT 取最新 CFA 报告。 */
export function getLatestCfaReport(
  companySymbol: string,
  asOf?: string,
): Promise<CfaReport | null> {
  return get<CfaReport | null>(
    '/api/research/reports/cfa/latest',
    {
      company_symbol: companySymbol,
      as_of: asOf || '',
    },
  );
}
