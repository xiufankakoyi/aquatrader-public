import service from './index';

export interface ResearchTaskRecord {
  task_id: string; task_type: string; trigger_source: string; target_claim_id: string;
  priority: number; status: string; progress: Record<string, unknown>; created_at: string;
}
export interface ClaimRecord {
  claim_id: string; subject_name: string; relation: string; object_name: string;
  research_status: string; evidence_confidence: number; trading_relevance: number;
  conflict_status: string; mapping_confidence: number; updated_at: string;
}
export interface ReviewRecord {
  review_id: string; claim_id: string; reason: string; priority: string; status: string;
  required_action: string; created_at: string;
}
export interface FeedbackRecord {
  feedback_id: string; domain: string; event_type: string; target_id: string;
  metrics: Record<string, unknown>; created_at: string;
}
export interface CalibrationRecord {
  calibration_id: string; domain: string; policy_version: string; status: string;
  sample_size: number; metrics: Record<string, unknown>; created_at: string;
}

async function list<T>(path: string, params?: Record<string, unknown>): Promise<T[]> {
  const response = await service.get(path, { params });
  return Array.isArray(response.data?.data) ? response.data.data : [];
}

export const getResearchTasks = (params?: Record<string, unknown>) => list<ResearchTaskRecord>('/api/research/tasks', params);
export const getResearchClaims = (params?: Record<string, unknown>) => list<ClaimRecord>('/api/research/claims', params);
export const getResearchReviews = (params?: Record<string, unknown>) => list<ReviewRecord>('/api/research/reviews', params);
export const getResearchFeedback = (params?: Record<string, unknown>) => list<FeedbackRecord>('/api/research/feedback', params);
export const getResearchCalibrations = (params?: Record<string, unknown>) => list<CalibrationRecord>('/api/research/calibrations', params);

export async function getClaimDetail(claimId: string): Promise<Record<string, any>> {
  const response = await service.get(`/api/research/claims/${encodeURIComponent(claimId)}`);
  return response.data?.data || {};
}

export async function resolveReview(reviewId: string, action: 'accept' | 'reject' | 'request_evidence', reviewer = 'web_user') {
  const response = await service.post(`/api/research/reviews/${encodeURIComponent(reviewId)}`, { action, reviewer });
  return response.data?.data || {};
}

// ---------------------------------------------------------------------------
// 阶段8: 投研驾驶舱增强 API
// ---------------------------------------------------------------------------

/** 事实结果记录。 */
export interface FactOutcomeRecord {
  record_id: string;
  transition_id: string;
  claim_id: string;
  promotion_type: string;
  from_status: string;
  to_status: string;
  fact_label: string;
  policy_version: string;
  promoted_at: string;
  resolved_at: string;
  resolved_by: string;
  created_at: string;
}

/** shadow 特征元信息（不含具体值，仅元数据）。 */
export interface ShadowFeaturesMeta {
  symbol: string;
  as_of: string;
  feature_names: string[];
  feature_version: string;
  policy_version: string;
  descriptions: Record<string, string>;
}

/** 市场时段状态。 */
export interface MarketSessionState {
  session: string;
  session_label: string;
  is_trading_time: boolean;
  is_trading_day: boolean;
  market_time: string;
  next_session: string;
}

/** 有效期分域配置。 */
export interface ValidityConfig {
  claim_types: Record<string, { days: number | null; label: string; name: string }>;
  evidence_types: Record<string, { days: number | null; label: string; name: string }>;
  policy_version: string;
  unknown_label: string;
}

/** 推送去重状态。 */
export interface PushDedupState {
  pushed_count: number;
  failures: Array<Record<string, unknown>>;
  failure_count: number;
  date: string;
  policy_version: string;
}

/**
 * 获取 Knowledge 域事实结果列表。
 *
 * @param params 查询参数：status=pending|resolved，limit。
 */
export const getFactOutcomes = (params?: Record<string, unknown>) =>
  list<FactOutcomeRecord>('/api/research/fact-outcomes', params);

/**
 * 获取 shadow 特征元信息（8 个特征名 + 版本）。
 *
 * @param params 查询参数：symbol、as_of 原样回显。
 */
export async function getShadowFeatures(params?: Record<string, unknown>): Promise<ShadowFeaturesMeta> {
  const response = await service.get('/api/research/shadow-features', { params });
  return response.data?.data || {} as ShadowFeaturesMeta;
}

/**
 * 获取当前 A 股市场时段状态。
 */
export async function getMarketSession(): Promise<MarketSessionState> {
  const response = await service.get('/api/research/market-session');
  return response.data?.data || {} as MarketSessionState;
}

/**
 * 获取有效期分域配置。
 */
export async function getValidityConfig(): Promise<ValidityConfig> {
  const response = await service.get('/api/research/validity-config');
  return response.data?.data || {} as ValidityConfig;
}

/**
 * 获取推送去重状态。
 *
 * @param params 查询参数：date、limit。
 */
export async function getPushDedup(params?: Record<string, unknown>): Promise<PushDedupState> {
  const response = await service.get('/api/research/push-dedup', { params });
  return response.data?.data || {} as PushDedupState;
}
