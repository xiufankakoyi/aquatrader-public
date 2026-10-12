/** ResearchCandidate API 客户端。 */
import service from './index';

interface ApiResponse<T> { ok: boolean; data?: T; error?: { message?: string } }

export interface ScoreBreakdown {
  score: { low: number; high: number };
  components: Record<string, number>;
  computability: string;
  reason: string;
}

export interface ResearchCandidate {
  candidate_id: string;
  symbol: string;
  sector_id: string;
  as_of: string;
  available_at: string;
  expires_at: string;
  thesis_id: string;
  thesis_status: string;
  evidence_confidence: { low: number; high: number };
  earnings_impact: Record<string, unknown>;
  expectation_gap: Record<string, unknown>;
  expectation_gap_confidence: { low: number; high: number };
  catalyst_score: ScoreBreakdown;
  catalyst_window: string;
  valuation_risk: { low: number; high: number };
  uncertainty_score: { low: number; high: number };
  research_score: ScoreBreakdown;
  alpha_score: ScoreBreakdown;
  core_score: ScoreBreakdown;
  weakest_assumption: string;
  invalidation_conditions: string[];
  next_review_at: string;
  source_trace: string[];
  decision_trace: string[];
  model_version: string;
  status: string;
}

export interface CandidateReport {
  candidate_id: string;
  symbol: string;
  report_sections: Record<string, string>;
  key_claims: Array<{ claim_id: string; source: string }>;
  evidence_references: Array<{ evidence_id: string; source: string }>;
  formula_references: Array<{ formula: string; section: string }>;
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

export const getHealth = () => get<Record<string, unknown>>('/api/research/candidates/health');
export const listCandidates = (symbol = '', status = '', limit = 100) =>
  get<ResearchCandidate[]>('/api/research/candidates', { symbol, status, limit });
export const buildCandidate = (body: Record<string, unknown>) =>
  post<ResearchCandidate>('/api/research/candidates/build', body);
export const getCandidate = (candidate_id: string) =>
  get<ResearchCandidate>(`/api/research/candidates/${candidate_id}`);
export const getCandidateReport = (candidate_id: string) =>
  get<CandidateReport>(`/api/research/candidates/${candidate_id}/report`);
