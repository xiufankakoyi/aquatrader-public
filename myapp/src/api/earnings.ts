/**
 * 盈利映射层 API 客户端。
 *
 * 对应后端 /api/research/earnings/* 端点。
 * 所有响应字段缺失时安全降级，不输出买卖/仓位/止盈止损建议。
 */
import service from './index';

/** 区间。 */
export interface Interval {
  low: number;
  high: number;
  mid: number;
  width: number;
}

/** 定量输入。 */
export interface QuantitativeInput {
  input_id: string;
  name: string;
  value: Interval;
  unit: string;
  currency: string;
  period: string;
  as_of: string;
  data_version: string;
  source_claim_id: string;
  source_evidence_id: string;
  extraction_method: string;
  confidence: Interval;
  published_at: string;
  retrieved_at: string;
  source_kind: string;
  notes: string;
}

/** 盈利驱动。 */
export interface EarningsDriver {
  driver_id: string;
  company_symbol: string;
  company_name: string;
  product_name: string;
  period: string;
  driver_type: string;
  description: string;
  evidence_ids: string[];
  claim_ids: string[];
  created_at: string;
}

/** 单情景结果。 */
export interface ScenarioResult {
  kind: string;
  new_demand: Interval;
  deliverable_volume: Interval;
  new_revenue: Interval;
  new_gross_profit: Interval;
  new_net_profit: Interval;
  eps_contribution: Interval;
  input_overrides: Record<string, unknown>;
  formula_trace: Record<string, unknown>;
}

/** 敏感性矩阵单项。 */
export interface SensitivityItem {
  input_name: string;
  base_value: number;
  low_value: number;
  high_value: number;
  eps_impact_low: number;
  eps_impact_high: number;
  elasticity: number;
}

/** 数据缺口。 */
export interface InputGap {
  gap_id: string;
  driver_id: string;
  field_name: string;
  reason: string;
  required_source_kind: string;
  severity: string;
  suggested_task: string;
}

/** 盈利影响结果。 */
export interface EarningsImpactResult {
  result_id: string;
  driver_id: string;
  company_symbol: string;
  company_name: string;
  product_name: string;
  period: string;
  as_of: string;
  status: string;
  currency: string;
  scenarios: ScenarioResult[];
  sensitivity_matrix: SensitivityItem[];
  gaps: InputGap[];
  base_inputs_snapshot: Record<string, unknown>;
  formula: string;
  data_version: string;
  created_at: string;
  exploration_hypothesis_blocked: Array<Record<string, unknown>>;
}

/** 健康检查响应。 */
export interface EarningsHealth {
  status: string;
  module: string;
  drivers: number;
  inputs: number;
  exposures: number;
  capitals: number;
  results: number;
  gaps: number;
}

/** 统一响应包装。 */
interface ApiResponse<T> {
  ok: boolean;
  data?: T;
  error?: { code: string; message: string };
  meta?: Record<string, unknown>;
}

async function _get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const response = await service.get<ApiResponse<T>>(path, { params });
  if (!response.data?.ok) {
    throw new Error(response.data?.error?.message || '请求失败');
  }
  return response.data.data as T;
}

async function _post<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  const response = await service.post<ApiResponse<T>>(path, body || {});
  if (!response.data?.ok) {
    throw new Error(response.data?.error?.message || '请求失败');
  }
  return response.data.data as T;
}

/** 健康检查。 */
export const getEarningsHealth = (): Promise<EarningsHealth> =>
  _get<EarningsHealth>('/api/research/earnings/health');

/** 查询驱动/模型列表。 */
export const listEarningsModels = (params?: Record<string, unknown>): Promise<EarningsDriver[]> =>
  _get<EarningsDriver[]>('/api/research/earnings/models', params);

/** 查询指定 driver 下的所有版本化模型容器。 */
export const listEarningsModelsByDriver = (params: { driver_id: string; model_type?: string; limit?: number }): Promise<Record<string, unknown>[]> =>
  _get<Record<string, unknown>[]>('/api/research/earnings/models/by_driver', params);

/** 查询单个模型容器详情。 */
export const getEarningsModel = (modelId: string): Promise<Record<string, unknown>> =>
  _get<Record<string, unknown>>(`/api/research/earnings/models/${modelId}`);

/** 查询定量输入列表。 */
export const listEarningsInputs = (params?: Record<string, unknown>): Promise<QuantitativeInput[]> =>
  _get<QuantitativeInput[]>('/api/research/earnings/inputs', params);

/** 查询情景列表。 */
export const listEarningsScenarios = (params?: Record<string, unknown>): Promise<Array<ScenarioResult & { result_id: string }>> =>
  _get<Array<ScenarioResult & { result_id: string }>>('/api/research/earnings/scenarios', params);

/** 查询结果列表。 */
export const listEarningsResults = (params?: Record<string, unknown>): Promise<EarningsImpactResult[]> =>
  _get<EarningsImpactResult[]>('/api/research/earnings/results', params);

/** 查询数据缺口。 */
export const listEarningsGaps = (params?: Record<string, unknown>): Promise<InputGap[]> =>
  _get<InputGap[]>('/api/research/earnings/gaps', params);

/** 执行盈利测算。 */
export const computeEarnings = (body: Record<string, unknown>): Promise<EarningsImpactResult> =>
  _post<EarningsImpactResult>('/api/research/earnings/compute', body);
