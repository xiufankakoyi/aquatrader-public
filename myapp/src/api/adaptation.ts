import service from './index';
interface Response<T> { ok: boolean; data?: T; error?: { message?: string } }
async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> { const r = await service.get<Response<T>>(path, { params }); if (!r.data.ok) throw new Error(r.data.error?.message || '请求失败'); return r.data.data as T; }
export interface FourDomainReport { report_id: string; as_of: string; pit_blocked_count: number; ic: number | null; rank_ic: number | null; grouped_returns: Record<string, number>; diagnostics: { industry_exposure: Record<string, number>; style_exposure: Record<string, number>; concentration_hhi: number; liquidity_utilization: number; capacity_utilization: number; violations: string[] }; cost_attribution: Record<string, number>; feedback_stats: Record<string, Record<string, number>>; }
export const listAdaptationReports = (as_of = '') => get<FourDomainReport[]>('/api/research/adaptation/reports', { as_of });
export const listDomainFeedback = (domain = '') => get<Record<string, unknown>[]>('/api/research/adaptation/feedback', { domain });
export const listAdaptationCalibrations = (domain = '') => get<Record<string, unknown>[]>('/api/research/adaptation/calibrations', { domain });
export const listAttributionReviews = (source_domain = '') => get<Record<string, unknown>[]>('/api/research/adaptation/reviews', { source_domain });
