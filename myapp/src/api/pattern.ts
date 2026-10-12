import service from './index';

export interface PatternTemplate {
  pattern_id: string;
  pattern_name: string;
  description: string;
  default_params: Record<string, number | string | boolean>;
  required_events: string[];
}

export interface EventSequenceItem {
  date: string;
  close?: number | null;
  change_pct?: number | null;
  events: string[];
}

export interface PatternMatch {
  pattern_id: string;
  pattern_name: string;
  symbol: string;
  stock_name?: string | null;
  match_date: string;
  event_sequence: EventSequenceItem[];
  match_score: number;
  hit_reasons: string[];
  risk_flags: string[];
  concept_tags: string[];
  future_return_1d?: number | null;
  future_return_3d?: number | null;
  future_return_5d?: number | null;
  future_return_10d?: number | null;
  max_gain_5d?: number | null;
  max_drawdown_5d?: number | null;
  success_label?: boolean | null;
  failure_reason?: string | null;
}

export interface PatternReport {
  pattern_id: string;
  pattern_name: string;
  start_date: string;
  end_date: string;
  params: Record<string, number | string | boolean>;
  summary: {
    total_matches: number;
    success_cases: number;
    failure_cases: number;
    current_candidates: number;
    success_rate: number | null;
  };
  results: PatternMatch[];
  success_samples: PatternMatch[];
  failure_samples: PatternMatch[];
  current_candidates: PatternMatch[];
  research_boundary: string;
  query_plan?: string;
  cache_hit?: boolean;
  meta?: Record<string, any>;
}

export interface PatternEventRow {
  stock_code: string;
  stock_name?: string | null;
  trade_date: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  change_pct?: number | null;
  volume?: number | null;
  amount?: number | null;
  ma5?: number | null;
  ma10?: number | null;
  [key: string]: unknown;
}

export interface PatternSearchMeta {
  elapsed_ms?: number;
  cache_hit?: boolean;
  query_plan?: string;
  applied_window?: { start_date: string; end_date: string; max_symbols: number; max_window_days: number };
  sync_limits?: { max_symbols: number; max_window_days: number };
  timeout_hint?: string;
  next_action?: string;
}

export interface PatternSearchResult {
  data: PatternReport;
  meta: PatternSearchMeta;
  raw: { success: boolean; data: PatternReport; meta?: PatternSearchMeta; error?: string };
}

/** job 模式状态 (POST /api/patterns/jobs / GET /api/patterns/jobs/<id>). */
export type PatternJobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface PatternJobState {
  job_id: string;
  status: PatternJobStatus;
  total: number;
  partial_count: number;
  progress: number;
  eta_seconds: number | null;
  items_partial: PatternMatch[];
  created_at?: number;
  updated_at?: number;
  finished_at?: number | null;
  error?: string | null;
  error_code?: string | null;
}

export interface PatternJobCreateResult {
  data: PatternJobState;
  meta: { query_plan: string; cache_hit: boolean };
}

export interface PatternJobResultResult {
  data: PatternReport;
  meta: { job: PatternJobState; cache_hit: boolean };
}

interface ApiResponse<T> {
  success: boolean;
  data: T;
  error?: string;
  code?: string;
  meta?: Record<string, any>;
}

export async function getPatternTemplates(): Promise<PatternTemplate[]> {
  const response = await service.get<ApiResponse<PatternTemplate[]>>('/api/patterns/templates');
  return response.data.data;
}

/**
 * 同步搜索: 默认 max_symbols=30, max_window_days=60; 超过 5s 时 meta.timeout_hint 给出
 * 切到 job 模式的建议; 前端据此决定是否要 fallback 到异步.
 */
export async function searchPatterns(payload: {
  pattern_id: string;
  start_date: string;
  end_date: string;
  symbols?: string[];
  params?: Record<string, number | string | boolean>;
  limit?: number;
  top_n?: number;
  auto_generate?: boolean;
  max_symbols?: number;
  max_window_days?: number;
}): Promise<PatternSearchResult> {
  const response = await service.post<ApiResponse<PatternReport>>('/api/patterns/search', payload);
  const body = response.data;
  return {
    data: body.data,
    meta: (body.meta as PatternSearchMeta) || {},
    raw: body as { success: boolean; data: PatternReport; meta?: PatternSearchMeta; error?: string },
  };
}

/**
 * 创建异步 job (T3.1). 返回 job_id, 供后续轮询.
 * 与 /api/patterns/search 不同: 不限 max_symbols / max_window_days, 走后台线程.
 */
export async function createPatternJob(payload: {
  pattern_id: string;
  start_date: string;
  end_date: string;
  symbols?: string[];
  params?: Record<string, number | string | boolean>;
  limit?: number;
  auto_generate?: boolean;
}): Promise<PatternJobCreateResult> {
  const response = await service.post<ApiResponse<PatternJobState>>('/api/patterns/jobs', payload);
  return {
    data: response.data.data,
    meta: (response.data.meta as { query_plan: string; cache_hit: boolean }) || { query_plan: 'live_scan_async', cache_hit: false },
  };
}

/** 查询 job 状态 (含 progress / eta_seconds / items_partial). */
export async function getPatternJob(jobId: string): Promise<PatternJobState> {
  const response = await service.get<ApiResponse<PatternJobState>>(`/api/patterns/jobs/${encodeURIComponent(jobId)}`);
  return response.data.data;
}

/** 拉取 job 完整结果 (status=succeeded 时). */
export async function getPatternJobResult(jobId: string): Promise<PatternJobResultResult> {
  const response = await service.get<ApiResponse<PatternReport>>(`/api/patterns/jobs/${encodeURIComponent(jobId)}/result`);
  return {
    data: response.data.data,
    meta: (response.data.meta as { job: PatternJobState; cache_hit: boolean }) || { job: {} as PatternJobState, cache_hit: false },
  };
}

/** 取消一个 running job (返回最新状态). */
export async function cancelPatternJob(jobId: string): Promise<PatternJobState> {
  const response = await service.post<ApiResponse<PatternJobState>>(`/api/patterns/jobs/${encodeURIComponent(jobId)}/cancel`);
  return response.data.data;
}

export async function getSymbolEvents(symbol: string, startDate: string, endDate: string): Promise<PatternEventRow[]> {
  const response = await service.get<ApiResponse<PatternEventRow[]>>(`/api/patterns/symbol/${encodeURIComponent(symbol)}/events`, {
    params: { start_date: startDate, end_date: endDate, limit: 1000 },
  });
  return response.data.data;
}
