import service from './index';

interface ApiResponse<T> { ok: boolean; data?: T; error?: { message?: string } }
export interface Thesis {
  thesis_id: string; company_symbol: string; title: string; status: string;
  core_assumptions: string[]; required_claim_ids: string[];
  invalidation_conditions: string[]; weakest_assumption: string;
  next_evidence_required: string; valid_until: string; review_at: string;
  catalysts: Array<{ description: string; window_start: string; window_end: string }>;
  contradictory_evidence_ids?: string[];
  supporting_evidence_ids?: string[];
  version?: number;
  audits?: Array<{ from_status: string; to_status: string; reason: string; created_at: string }>;
}

export interface MarketExpectation {
  expectation_id: string; symbol: string;
  expected_revenue: { low: number; high: number };
  expected_net_profit: { low: number; high: number };
  expected_EPS: { low: number; high: number };
  valuation_multiple: { low: number; high: number };
  expectation_source: string; source_count: number;
  available_at: string; forecast_period: string;
  confidence: { low: number; high: number };
  evidence_ids: string[]; version: string;
}

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const response = await service.get<ApiResponse<T>>(path, { params });
  if (!response.data?.ok) throw new Error(response.data?.error?.message || '请求失败');
  return response.data.data as T;
}
async function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await service.post<ApiResponse<T>>(path, body);
  if (!response.data?.ok) throw new Error(response.data?.error?.message || '请求失败');
  return response.data.data as T;
}
export const listGuidance = (company_symbol = '') => get<Record<string, unknown>[]>('/api/research/expectations/guidance', { company_symbol });
export const listConsensus = (company_symbol = '') => get<Record<string, unknown>[]>('/api/research/expectations/consensus', { company_symbol });
export const listImplied = (company_symbol = '') => get<Record<string, unknown>[]>('/api/research/expectations/implied', { company_symbol });
export const listMarketExpectations = (symbol = '') => get<MarketExpectation[]>('/api/research/expectations/market_expectations', { symbol });
export const createMarketExpectation = (body: Record<string, unknown>) => post<MarketExpectation>('/api/research/expectations/market_expectations', body);
export const reverseImplied = (body: Record<string, unknown>) => post<MarketExpectation>('/api/research/expectations/implied_reverse', body);
export const listGaps = (company_symbol = '') => get<Record<string, unknown>[]>('/api/research/expectations/gaps', { company_symbol });
export const listTheses = (company_symbol = '') => get<Thesis[]>('/api/research/expectations/theses', { company_symbol });
export const createThesis = (body: Record<string, unknown>) => post<Thesis>('/api/research/expectations/theses', body);
export const evaluateThesisClaims = (thesis_id: string, claim_states: Record<string, string>) =>
  post<{ thesis: Thesis; reviews: unknown[] }>(`/api/research/expectations/theses/${thesis_id}/evaluate-claims`, { claim_states });
export const addContradiction = (thesis_id: string, evidence_id: string, reason = '') =>
  post<{ thesis: Thesis; reviews: unknown[] }>(`/api/research/expectations/theses/${thesis_id}/add-contradiction`, { evidence_id, reason });
export const checkThesisExpiry = (thesis_id: string, as_of = '') =>
  post<{ thesis: Thesis; reviews: unknown[] }>(`/api/research/expectations/theses/${thesis_id}/check-expiry`, { as_of });
export const promoteThesis = (thesis_id: string, action: string, params: Record<string, unknown> = {}) =>
  post<{ thesis: Thesis; reviews: unknown[] }>(`/api/research/expectations/theses/${thesis_id}/promote`, { action, ...params });
