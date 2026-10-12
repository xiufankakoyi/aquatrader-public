/**
 * P3.7 机构级研究工作台 API 客户端。
 *
 * 对接 server/routes/research_model_routes.py 暴露的 9 个工作台只读端点：
 *   GET /api/research/models/workbench/overview           工作台总览
 *   GET /api/research/models/workbench/discovery-funnel   全市场发现漏斗
 *   GET /api/research/models/workbench/company-cards      公司研究卡和历史版本
 *   GET /api/research/models/workbench/profit-models      产品利润模型和情景
 *   GET /api/research/models/workbench/variant-perception VariantPerception
 *   GET /api/research/models/workbench/bear-questions      反方问题与投委会
 *   GET /api/research/models/workbench/risk-calendar       风险和验证日历
 *   GET /api/research/models/workbench/data-health         数据健康与降级状态
 *   GET /api/research/models/workbench/scorecard           历史预测记分卡
 *
 * 同时复用 P3.1 已有的模型治理写端点（注册、转移、回滚、暂停、门禁）。
 *
 * 严格遵循 P3.7 传播治理规则：
 * - 不输出买入/卖出/仓位/止盈/止损等交易动作措辞
 * - 字段缺失时返回降级空数据结构，不让页面崩溃
 * - 每份响应包含 as_of / model_version / source / limitations
 */
import service from './index';

/** 统一响应包装。 */
interface ApiResponse<T> {
  success?: boolean;
  data?: T;
  error?: string;
}

/** 工作台响应统一元数据。 */
export interface WorkbenchMeta {
  as_of: string;
  model_version: string;
  source: string;
  limitations: string[];
  degraded?: boolean;
  degradation_reason?: string;
}

/** 漏斗阶段。 */
export interface FunnelStage {
  stage: string;
  label: string;
  count: number;
  passed: boolean;
}

/** 发现漏斗响应。 */
export interface DiscoveryFunnelResponse extends WorkbenchMeta {
  stages: FunnelStage[];
  total_passed: number;
}

/** 公司研究卡。 */
export interface CompanyCard {
  symbol: string;
  company_name: string;
  industry: string;
  latest_as_of: string;
  version_count: number;
  latest_status: string;
  versions: Array<{
    run_id: string;
    as_of: string;
    status: string;
    created_at: string;
    schema_version: string;
  }>;
}

/** 公司研究卡列表响应。 */
export interface CompanyCardsResponse extends WorkbenchMeta {
  cards: CompanyCard[];
  total: number;
  symbol_filter: string;
}

/** 利润模型情景。 */
export interface ProfitScenario {
  name: string;
  base_case: number | null;
  bull_case: number | null;
  bear_case: number | null;
  assumptions: string[];
  fact_basis_ids: string[];
}

/** 利润模型响应。 */
export interface ProfitModelsResponse extends WorkbenchMeta {
  scenarios: ProfitScenario[];
  fact_basis: Array<Record<string, unknown>>;
  assumption_basis: Array<Record<string, unknown>>;
}

/** VariantPerception 卡片。 */
export interface VariantPerceptionCard {
  perception_id: string;
  symbol: string;
  company_name: string;
  as_of: string;
  status: string;
  market_view: Record<string, unknown> | null;
  internal_view: Record<string, unknown> | null;
  driver_differences: Array<Record<string, unknown>>;
  catalysts: Array<Record<string, unknown>>;
  invalidation: Array<Record<string, unknown>>;
  review_status: string;
  version: string;
}

/** VariantPerception 响应。 */
export interface VariantPerceptionResponse extends WorkbenchMeta {
  perceptions: VariantPerceptionCard[];
  active_count: number;
}

/** 反方问题。 */
export interface BearQuestion {
  question_id: string;
  symbol: string;
  question: string;
  raised_by: string;
  raised_at: string;
  status: string;
  answer: string;
  answered_by: string;
  answered_at: string;
}

/** 投委会状态。 */
export interface CommitteeStatus {
  state: string;
  pending_questions: number;
  last_convened_at: string;
  members: Array<Record<string, unknown>>;
}

/** 反方问题与投委会响应。 */
export interface BearQuestionsResponse extends WorkbenchMeta {
  bear_questions: BearQuestion[];
  committee_status: CommitteeStatus;
}

/** 风险日历事件。 */
export interface RiskCalendarEvent {
  event_id: string;
  date: string;
  type: string;
  title: string;
  status: string;
  source: string;
  symbol: string;
  trade_action_free: boolean;
  description: string;
}

/** 风险日历响应。 */
export interface RiskCalendarResponse extends WorkbenchMeta {
  events: RiskCalendarEvent[];
  upcoming_count: number;
}

/** 数据健康响应。 */
export interface DataHealthResponse extends WorkbenchMeta {
  data_health: Record<string, unknown>;
  model_status_summary: {
    active_count: number;
    suspended_count: number;
    any_suspended: boolean;
  };
}

/** 记分卡条目。 */
export interface ScorecardItem {
  prediction_id: string;
  symbol: string;
  metric: string;
  period: string;
  predicted: number | null;
  actual: number | null;
  error: number | null;
  coverage_rate: number | null;
  hit_status: string;
  predicted_at: string;
  resolved_at: string;
}

/** 记分卡响应。 */
export interface ScorecardResponse extends WorkbenchMeta {
  scorecard: ScorecardItem[];
  summary: {
    total_predictions: number;
    hit_count: number;
    miss_count: number;
    coverage_rate: number | null;
    mean_absolute_error: number | null;
  };
}

/** 模型版本（来自 P3.1）。 */
export interface ModelVersionRecord {
  model_id: string;
  model_family: string;
  domain: string;
  version: string;
  owner: string;
  reviewer: string;
  training_or_calibration_window: string;
  input_contract: Record<string, unknown>;
  output_contract: Record<string, unknown>;
  applicable_universe: string[];
  limitations: string[];
  benchmark: Record<string, unknown>;
  validation_report: Record<string, unknown>;
  status: string;
  effective_from: string;
  effective_to: string;
  rollback_version: string;
  code_fingerprint: string;
  config_fingerprint: string;
  data_fingerprint: string;
  created_at: string;
  last_transition_at: string;
  last_transition_by: string;
}

/** 模型列表响应（来自 P3.1）。 */
export interface ModelListResponse {
  success: boolean;
  data: ModelVersionRecord[];
}

/** P3 门禁结果。 */
export interface P3GateResult {
  status: string;
  p2_overall_passed: boolean;
  reason: string;
  artifact_path: string;
  is_blocked: boolean;
}

/** 工作台总览响应。 */
export interface WorkbenchOverviewResponse extends WorkbenchMeta {
  p3_gate: P3GateResult;
  model_registry_summary: {
    model_families: number;
    total_versions: number;
  };
  modules: Array<{
    key: string;
    label: string;
    degraded: boolean;
  }>;
}

/** API 抛错携带的元信息。 */
export class ResearchWorkbenchApiError extends Error {
  code: string;
  status?: number;

  constructor(message: string, code = 'UNKNOWN', status?: number) {
    super(message);
    this.name = 'ResearchWorkbenchApiError';
    this.code = code;
    this.status = status;
  }
}

/** 通用 GET 包装：失败时抛出 ResearchWorkbenchApiError。 */
async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const response = await service.get<ApiResponse<T>>(path, { params });
  if (!response.data?.success) {
    throw new ResearchWorkbenchApiError(
      response.data?.error || '请求失败',
      'UNKNOWN',
      response.status,
    );
  }
  return response.data.data as T;
}

/** 通用 POST 包装：失败时抛出 ResearchWorkbenchApiError。 */
async function post<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  const response = await service.post<ApiResponse<T>>(path, body || {});
  if (!response.data?.success) {
    throw new ResearchWorkbenchApiError(
      response.data?.error || '请求失败',
      'UNKNOWN',
      response.status,
    );
  }
  return response.data.data as T;
}

/** 安全调用：失败时返回 fallback 而非抛错。 */
async function safeCall<T>(promise: Promise<T>, fallback: T): Promise<T> {
  try {
    return await promise;
  } catch (e) {
    console.warn('[ResearchWorkbench] API 调用失败，使用降级数据', e);
    return fallback;
  }
}

// ---------------------------------------------------------------------------
// 工作台只读端点
// ---------------------------------------------------------------------------

/** 工作台总览（聚合 P3 门禁 + 各模块降级状态）。 */
export function getWorkbenchOverview(): Promise<WorkbenchOverviewResponse> {
  return get<WorkbenchOverviewResponse>('/api/research/models/workbench/overview');
}

/** 全市场发现漏斗。 */
export function getDiscoveryFunnel(): Promise<DiscoveryFunnelResponse> {
  return get<DiscoveryFunnelResponse>('/api/research/models/workbench/discovery-funnel');
}

/** 公司研究卡和历史版本列表。 */
export function getCompanyCards(symbol?: string): Promise<CompanyCardsResponse> {
  const params = symbol ? { symbol } : undefined;
  return get<CompanyCardsResponse>('/api/research/models/workbench/company-cards', params);
}

/** 产品利润模型和情景列表。 */
export function getProfitModels(): Promise<ProfitModelsResponse> {
  return get<ProfitModelsResponse>('/api/research/models/workbench/profit-models');
}

/** VariantPerception 卡片列表。 */
export function getVariantPerception(): Promise<VariantPerceptionResponse> {
  return get<VariantPerceptionResponse>('/api/research/models/workbench/variant-perception');
}

/** 反方问题和投委会状态。 */
export function getBearQuestions(): Promise<BearQuestionsResponse> {
  return get<BearQuestionsResponse>('/api/research/models/workbench/bear-questions');
}

/** 风险和验证日历。 */
export function getRiskCalendar(): Promise<RiskCalendarResponse> {
  return get<RiskCalendarResponse>('/api/research/models/workbench/risk-calendar');
}

/** 数据健康和降级状态。 */
export function getWorkbenchDataHealth(): Promise<DataHealthResponse> {
  return get<DataHealthResponse>('/api/research/models/workbench/data-health');
}

/** 历史预测与实际兑现记分卡。 */
export function getScorecard(): Promise<ScorecardResponse> {
  return get<ScorecardResponse>('/api/research/models/workbench/scorecard');
}

// ---------------------------------------------------------------------------
// P3.1 模型治理端点（已有 API，前端封装层统一暴露）
// ---------------------------------------------------------------------------

/** 列出所有模型版本；支持 status / domain / model_id 过滤。 */
export async function listModels(params: {
  status?: string;
  domain?: string;
  model_id?: string;
} = {}): Promise<ModelVersionRecord[]> {
  const response = await service.get<ModelListResponse>(
    '/api/research/models',
    { params },
  );
  if (!response.data?.success) {
    throw new ResearchWorkbenchApiError(
      response.data?.error || '请求失败',
      'UNKNOWN',
      response.status,
    );
  }
  return response.data.data || [];
}

/** 获取单个模型版本。 */
export function getModel(modelId: string, version: string): Promise<ModelVersionRecord> {
  return get<ModelVersionRecord>(
    `/api/research/models/${encodeURIComponent(modelId)}/${encodeURIComponent(version)}`,
  );
}

/** 注册新模型草稿。 */
export function registerModel(payload: Record<string, unknown>): Promise<ModelVersionRecord> {
  return post<ModelVersionRecord>('/api/research/models', payload);
}

/** 模型状态转移。 */
export function transitionModel(
  modelId: string,
  version: string,
  payload: {
    target_status: string;
    actor: string;
    report?: Record<string, unknown>;
    reviewer?: string;
    rollback_version?: string;
    reason?: string;
  },
): Promise<ModelVersionRecord> {
  return post<ModelVersionRecord>(
    `/api/research/models/${encodeURIComponent(modelId)}/${encodeURIComponent(version)}/transition`,
    payload as Record<string, unknown>,
  );
}

/** 模型回滚。 */
export function rollbackModel(
  modelId: string,
  version: string,
  payload: { actor: string; reason: string },
): Promise<ModelVersionRecord> {
  return post<ModelVersionRecord>(
    `/api/research/models/${encodeURIComponent(modelId)}/${encodeURIComponent(version)}/rollback`,
    payload as Record<string, unknown>,
  );
}

/** 输入契约变化暂停相关模型。 */
export function suspendOnContractChange(payload: {
  old_contract_hash: string;
  new_contract_hash: string;
  actor: string;
  reason?: string;
}): Promise<{
  suspended_count: number;
  suspended: ModelVersionRecord[];
}> {
  return post<{
    suspended_count: number;
    suspended: ModelVersionRecord[];
  }>('/api/research/models/contract-change/suspend', payload as Record<string, unknown>);
}

/** P3 门禁状态查询。 */
export async function getP3Gate(): Promise<P3GateResult> {
  const response = await service.get<ApiResponse<P3GateResult>>(
    '/api/research/models/gate',
  );
  if (!response.data?.success) {
    throw new ResearchWorkbenchApiError(
      response.data?.error || '请求失败',
      'UNKNOWN',
      response.status,
    );
  }
  return response.data.data as P3GateResult;
}

// ---------------------------------------------------------------------------
// 降级包装：所有只读端点都提供「失败时返回降级空数据」版本
// ---------------------------------------------------------------------------

/** 安全获取工作台总览。 */
export function safeGetWorkbenchOverview(): Promise<WorkbenchOverviewResponse> {
  return safeCall(getWorkbenchOverview(), makeEmptyOverview());
}

/** 安全获取发现漏斗。 */
export function safeGetDiscoveryFunnel(): Promise<DiscoveryFunnelResponse> {
  return safeCall(getDiscoveryFunnel(), makeEmptyDiscoveryFunnel());
}

/** 安全获取公司研究卡。 */
export function safeGetCompanyCards(symbol?: string): Promise<CompanyCardsResponse> {
  return safeCall(getCompanyCards(symbol), makeEmptyCompanyCards());
}

/** 安全获取利润模型。 */
export function safeGetProfitModels(): Promise<ProfitModelsResponse> {
  return safeCall(getProfitModels(), makeEmptyProfitModels());
}

/** 安全获取 VariantPerception。 */
export function safeGetVariantPerception(): Promise<VariantPerceptionResponse> {
  return safeCall(getVariantPerception(), makeEmptyVariantPerception());
}

/** 安全获取反方问题。 */
export function safeGetBearQuestions(): Promise<BearQuestionsResponse> {
  return safeCall(getBearQuestions(), makeEmptyBearQuestions());
}

/** 安全获取风险日历。 */
export function safeGetRiskCalendar(): Promise<RiskCalendarResponse> {
  return safeCall(getRiskCalendar(), makeEmptyRiskCalendar());
}

/** 安全获取数据健康。 */
export function safeGetWorkbenchDataHealth(): Promise<DataHealthResponse> {
  return safeCall(getWorkbenchDataHealth(), makeEmptyDataHealth());
}

/** 安全获取记分卡。 */
export function safeGetScorecard(): Promise<ScorecardResponse> {
  return safeCall(getScorecard(), makeEmptyScorecard());
}

/** 安全列出模型。 */
export async function safeListModels(params: {
  status?: string;
  domain?: string;
  model_id?: string;
} = {}): Promise<ModelVersionRecord[]> {
  return safeCall(listModels(params), []);
}

/** 安全获取 P3 门禁状态。 */
export async function safeGetP3Gate(): Promise<P3GateResult> {
  return safeCall(getP3Gate(), {
    status: 'unknown',
    p2_overall_passed: false,
    reason: 'P3 门禁查询失败',
    artifact_path: '',
    is_blocked: true,
  });
}

// ---------------------------------------------------------------------------
// 降级空数据工厂
// ---------------------------------------------------------------------------

function makeBaseMeta(degradedReason: string): WorkbenchMeta {
  return {
    as_of: new Date().toISOString(),
    model_version: 'p3-workbench-v1',
    source: 'researchWorkbench.ts.fallback',
    limitations: ['前端降级空数据：API 调用失败'],
    degraded: true,
    degradation_reason: degradedReason,
  };
}

function makeEmptyOverview(): WorkbenchOverviewResponse {
  return {
    ...makeBaseMeta('工作台总览查询失败'),
    p3_gate: {
      status: 'unknown',
      p2_overall_passed: false,
      reason: 'P3 门禁查询失败',
      artifact_path: '',
      is_blocked: true,
    },
    model_registry_summary: { model_families: 0, total_versions: 0 },
    modules: [],
  };
}

function makeEmptyDiscoveryFunnel(): DiscoveryFunnelResponse {
  return {
    ...makeBaseMeta('发现漏斗查询失败'),
    stages: [],
    total_passed: 0,
  };
}

function makeEmptyCompanyCards(): CompanyCardsResponse {
  return {
    ...makeBaseMeta('公司研究卡查询失败'),
    cards: [],
    total: 0,
    symbol_filter: '',
  };
}

function makeEmptyProfitModels(): ProfitModelsResponse {
  return {
    ...makeBaseMeta('利润模型查询失败'),
    scenarios: [],
    fact_basis: [],
    assumption_basis: [],
  };
}

function makeEmptyVariantPerception(): VariantPerceptionResponse {
  return {
    ...makeBaseMeta('VariantPerception 查询失败'),
    perceptions: [],
    active_count: 0,
  };
}

function makeEmptyBearQuestions(): BearQuestionsResponse {
  return {
    ...makeBaseMeta('反方问题查询失败'),
    bear_questions: [],
    committee_status: {
      state: 'not_convened',
      pending_questions: 0,
      last_convened_at: '',
      members: [],
    },
  };
}

function makeEmptyRiskCalendar(): RiskCalendarResponse {
  return {
    ...makeBaseMeta('风险日历查询失败'),
    events: [],
    upcoming_count: 0,
  };
}

function makeEmptyDataHealth(): DataHealthResponse {
  return {
    ...makeBaseMeta('数据健康查询失败'),
    data_health: {},
    model_status_summary: {
      active_count: 0,
      suspended_count: 0,
      any_suspended: false,
    },
  };
}

function makeEmptyScorecard(): ScorecardResponse {
  return {
    ...makeBaseMeta('记分卡查询失败'),
    scorecard: [],
    summary: {
      total_predictions: 0,
      hit_count: 0,
      miss_count: 0,
      coverage_rate: null,
      mean_absolute_error: null,
    },
  };
}
