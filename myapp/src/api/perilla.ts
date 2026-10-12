import service from './index';

/** 证据边图谱节点（ECharts graph 格式） */
export interface EvidenceGraphNode {
  name: string;
  category: number;
  value: number;
  symbolSize: number;
}

/** 证据边图谱链接 */
export interface EvidenceGraphLink {
  source: string;
  target: string;
  relation: string;
  confidence_level: string;
  evidence_count: number;
  evidence_quotes: string[];
}

/** 证据边图谱分类 */
export interface EvidenceGraphCategory {
  name: string;
}

/** 证据边图谱完整数据 */
export interface EvidenceGraphData {
  nodes: EvidenceGraphNode[];
  links: EvidenceGraphLink[];
  categories: EvidenceGraphCategory[];
}

/** 候选概念 */
export interface CandidateConcept {
  concept_id: string;
  concept_name: string;
  normalized_name: string;
  term_type: string;
  root_theme: string;
  chain_layer: string;
  evidence_level: string;
  concept_score: number;
  co_terms: string[];
  evidence_document_ids: string[];
  discovery_reason: string;
  llm_explanation?: string;
  related_companies?: string[];
}

/** 原始术语 */
export interface RawTerm {
  term: string;
  normalized_term: string;
  term_type: string;
  source_count: number;
  recent_count: number;
  burst_score: number;
  co_terms: string[];
  status: string;
}

/** 调度器状态 */
export interface SchedulerStatus {
  enabled: boolean;
  started: boolean;
  running: boolean;
  run_on_startup: boolean;
  startup_delay_seconds: number;
  schedule: {
    hour: number;
    minute: number;
    timezone: string;
    skip_weekends: boolean;
  };
  llm: {
    use_llm: boolean;
    max_llm_terms: number;
    max_execute_tasks: number;
    execute_search: boolean;
  };
  next_run_at: string | null;
  last_run_at: string | null;
  last_finished_at: string | null;
  last_status: string;
  last_error: string;
  last_summary: Record<string, unknown> | null;
}

/** 获取证据边图谱 */
export async function getEvidenceEdges(params?: {
  limit?: number;
  relation?: string;
  min_evidence?: number;
}): Promise<{ data: EvidenceGraphData; meta: Record<string, unknown> }> {
  const res = await service.get('/api/perilla/evidence-edges', { params });
  return { data: res.data.data, meta: res.data.meta };
}

/** 获取候选概念列表 */
export async function getCandidateConcepts(params?: {
  limit?: number;
  root_theme?: string;
}): Promise<{ data: CandidateConcept[]; meta: Record<string, unknown> }> {
  const res = await service.get('/api/perilla/candidate-concepts', { params });
  return { data: res.data.data, meta: res.data.meta };
}

/** 获取原始术语列表 */
export async function getRawTerms(params?: { limit?: number }): Promise<{
  data: RawTerm[];
  meta: Record<string, unknown>;
}> {
  const res = await service.get('/api/perilla/raw-terms', { params });
  return { data: res.data.data, meta: res.data.meta };
}

/** 获取今日新词 markdown */
export async function getNewTermsToday(): Promise<{
  markdown: string;
  generated_at: string;
  size_bytes: number;
}> {
  const res = await service.get('/api/perilla/new-terms-today');
  return res.data.data;
}

/** 获取日报 markdown */
export async function getDailyBrief(): Promise<{
  markdown: string;
  generated_at: string;
  size_bytes: number;
}> {
  const res = await service.get('/api/perilla/daily-brief');
  return res.data.data;
}

/** 获取调度器状态 */
export async function getSchedulerStatus(): Promise<SchedulerStatus> {
  const res = await service.get('/api/perilla/scheduler-status');
  return res.data.data;
}

/** 知识链增长过程单层数据 */
export interface GrowthLayer {
  layer: number;
  title: string;
  count: number;
  detail: string;
  status: 'pending' | 'running' | 'done';
  breakdown?: Record<string, number>;
}

/** 冷门新词 */
export interface SelectedNewTerm {
  normalized_name: string;
  term_type: string;
  root_theme: string;
  evidence_level: string;
  concept_score: number;
  [key: string]: unknown;
}

/** 知识链增长过程完整数据 */
export interface GrowthProcessData {
  layers: GrowthLayer[];
  sag_stats: Record<string, unknown>;
  promotion_candidates: Record<string, unknown>[];
  selected_new_term: SelectedNewTerm;
  task_runs: Record<string, unknown>[];
  generated_at: string;
}

/** 获取知识链增长过程与最终结果 */
export async function getGrowthProcess(): Promise<{ data: GrowthProcessData }> {
  const res = await service.get('/api/perilla/growth-process');
  return { data: res.data.data };
}

/** 获取 SAG 统计与晋升候选 */
export async function getSagStats(): Promise<{
  data: { sag_stats: Record<string, unknown>; promotion_candidates: Record<string, unknown>[] };
}> {
  const res = await service.get('/api/perilla/sag-stats');
  return { data: res.data.data };
}

/** 触发日报生成（异步） */
export async function runDailyBrief(params?: {
  skip_perilla?: boolean;
  skip_research?: boolean;
  execute_search?: boolean;
  use_llm?: boolean;
}): Promise<{ task_id: string; status: string }> {
  const res = await service.post('/api/perilla/run-daily-brief', params || {});
  return res.data.data;
}
