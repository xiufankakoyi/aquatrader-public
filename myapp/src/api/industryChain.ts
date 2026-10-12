import service from './index';

export interface ChainInfo {
  chain_id: string;
  name: string;
  aliases: string[];
  description: string;
  node_count: number;
  source?: string;
  is_fallback?: boolean;
}

/**
 * 产业链图节点（兼容 T3.4 新规范）。
 * 字段: { id, name, type: "theme|stock|industry|event", score, data_date, evidence_count, source }
 * 兼容字段: x / y / layer / hot_score / importance / market_strength / stock_count / ...
 */
export interface ChainNode {
  id: string;
  name: string;
  type: string;
  /** 兼容字段：若为 yaml 旧节点，类型可能是 product/component/material/... */
  layer?: string;
  layer_name?: string;
  order?: number;
  aliases?: string[];
  keywords?: string[];
  description?: string;
  /** 新字段：节点 score（热度 / 强度） */
  score?: number;
  /** 新字段：节点关联的数据日期 */
  data_date?: string;
  /** 新字段：节点关联的证据条数 */
  evidence_count?: number;
  /** 新字段：节点的数据源标识 */
  source?: string;
  /** 兼容字段 */
  x?: number;
  y?: number;
  fixed?: boolean;
  importance?: number;
  hot_score?: number;
  hot_score_source?: string;
  market_strength?: string;
  stock_count?: number;
  candidate_count?: number;
  verified_stock_count?: number;
  candidate_stock_count?: number;
  limit_up_count?: number;
  avg_return_1d?: number;
  avg_pct_chg?: number;
  avg_return_5d?: number;
  total_amount?: number;
  main_net_inflow?: number;
  updated_at?: string;
  provider_summary?: string;
  symbolSize?: number | [number, number];
  itemStyle?: Record<string, any>;
  label?: Record<string, any>;
  is_fallback?: boolean;
  demo_highlight?: boolean;
}

/**
 * 产业链图边（兼容 T3.4 新规范）。
 * 字段: { source, target, relation_type: "belongs_to|drives|conflicts_with|evidence", weight, evidence: string[] }
 * 兼容字段: relation / label / lineStyle / description
 */
export interface ChainEdge {
  source: string;
  target: string;
  /** 新字段：关系类型 */
  relation_type?: string;
  /** 新字段：边权重 0~1 */
  weight?: number;
  /** 新字段：证据描述列表 */
  evidence?: string[];
  /** 兼容字段 */
  relation?: string;
  label?: Record<string, any>;
  lineStyle?: Record<string, any>;
  description?: string;
}

export interface ChainLayer {
  id: string;
  name: string;
  order: number;
}

export interface ChainSummary {
  chain_id: string;
  chain_name: string;
  hot_score: number;
  market_strength: string;
  top_node: string;
  top_node_name: string;
  limit_up_count: number;
  turnover_change: number;
  node_count: number;
  stock_count: number;
  updated_at?: string;
}

/**
 * T3.4 /graph 端点统一响应契约。
 */
export interface GraphPayload {
  nodes: ChainNode[];
  links: ChainEdge[];
  layers?: ChainLayer[];
  summary?: ChainSummary;
  /** 数据日期 (YYYY-MM-DD) */
  data_date: string;
  /** 实际命中的数据源 */
  source: string;
  has_data: boolean;
  demo_mode: boolean;
  missing_sources?: string[];
}

export interface GraphMeta {
  endpoint: string | null;
  data_date: string | null;
  has_data: boolean;
  source: string;
  missing_sources: string[];
  partial_success: boolean;
  demo_mode: boolean;
  /** stale = data_date 与当前日期相差 > 1 个交易日 */
  stale: boolean;
  query_plan: string[];
  elapsed_ms: number;
}

/**
 * 兼容旧版 ChainGraphData（保持同名导出）。
 */
export interface ChainGraphData {
  nodes: ChainNode[];
  edges: ChainEdge[];
  layers: ChainLayer[];
  summary: ChainSummary;
  source?: string;
  is_fallback?: boolean;
  data_date?: string;
  has_data?: boolean;
  demo_mode?: boolean;
  missing_sources?: string[];
}

export interface NodeDetail {
  node: {
    id: string;
    name: string;
    type: string;
    layer: string;
    layer_name: string;
    order: number;
    aliases: string[];
    keywords: string[];
    description: string;
    importance: number;
  };
  upstream: { id: string; name: string; type: string }[];
  downstream: { id: string; name: string; type: string }[];
  metrics: Record<string, any>;
  stock_count: number;
  stocks: ChainStock[];
}

export interface ChainStock {
  chain_id: string;
  node_id: string;
  symbol: string;
  stock_name: string;
  chain_position: string;
  source: string;
  data_source: string;
  source_concept_name: string;
  matched_board_name: string;
  matched_keyword: string;
  candidate_source: string;
  provider: string;
  is_verified: boolean;
  is_candidate: boolean;
  is_manual_override: boolean;
  flag_label: string;
  relevance_score: number;
  purity_score: number;
  evidence_score: number;
  market_confirm_score: number;
  final_score: number;
  system_relevance_score: number;
  source_confidence: number;
  auto_relevance_score: number;
  evidence_type: string;
  evidence_text: string;
  evidence_source: string;
  updated_at: string;
  pct_chg: number | null;
  return_5d: number | null;
  amount: number | null;
  amount_change_ratio: number | null;
  turnover_rate?: number | null;
  volume_ratio?: number | null;
  limit_status: string | null;
  is_limit_up: boolean;
  consecutive_limit_count: number;
  limit_up_reason: string;
  main_net_inflow: number | null;
  market_provider?: string;
  fund_flow_provider?: string;
  pattern_signal: string | null;
}

export interface NodeStocksResponse {
  rows: ChainStock[];
  verified_stocks: ChainStock[];
  candidate_stocks: ChainStock[];
  evidence_summary: {
    evidence_count: number;
    verified_count: number;
    evidence_types: string[];
    latest_update: string | null;
  };
  market_metrics: Record<string, any>;
  message?: string;
}

export interface DataSourceStatus {
  providers: Record<string, Record<string, any>>;
  last_sync: string | null;
  parquet_files: Record<string, boolean>;
  parquet_row_counts?: Record<string, number>;
  recent_source_log?: Record<string, any>[];
  auto_update_scheduler?: Record<string, any>;
  empty_hint?: string;
}

export interface IndustryChainDebug {
  project_root: string;
  knowledge_path: string;
  industry_chain_files: string[];
  loaded_chains: string[];
  optical_communication_exists: boolean;
  node_count: number;
  edge_count: number;
  stock_mapping_count: number;
}

export interface SearchResult {
  chains: { chain_id: string; name: string; aliases: string[]; description: string }[];
  nodes: {
    chain_id: string;
    chain_name: string;
    node_id: string;
    node_name: string;
    type: string;
    layer: string;
    layer_name: string;
    aliases: string[];
    keywords: string[];
    description: string;
  }[];
}

/**
 * T3.4 /graph 端点原始响应（meta + data 形态）。
 */
export interface GraphResponse {
  success: boolean;
  data: GraphPayload | null;
  meta: GraphMeta | null;
  error: { code: string; message: string; detail: string; retryable: boolean } | null;
}

interface ApiResponse<T> {
  success: boolean;
  data: T;
  message?: string;
  error?: string;
  research_boundary?: string;
}

export async function listChains(): Promise<ChainInfo[]> {
  const response = await service.get<ApiResponse<ChainInfo[]>>('/api/industry-chain/chains');
  return response.data.data;
}

/**
 * T3.4 拉取产业链 / 题材支持网图谱。
 * - 返回 GraphResponse, 由调用方决定如何渲染（Empty / Error / 完整图谱）
 * - 错误由 meta.stale / meta.has_data / meta.missing_sources 等字段透传
 */
export async function getChainGraph(
  params: { chainId?: string; tradeDate?: string; dataDate?: string; theme?: string } = {}
): Promise<GraphResponse> {
  const query: Record<string, string> = {};
  if (params.chainId) query.chain_id = params.chainId;
  if (params.theme) query.theme = params.theme;
  if (params.tradeDate) query.trade_date = params.tradeDate;
  if (params.dataDate) query.data_date = params.dataDate;
  const response = await service.get<GraphResponse>('/api/industry-chain/graph', { params: query });
  return response.data;
}

/**
 * 兼容旧版调用：返回 ChainGraphData（旧结构）。
 * 注意：仅保留向后兼容，缺失字段会以空值占位。
 */
export async function getChainGraphLegacy(
  chainId: string,
  tradeDate?: string
): Promise<ChainGraphData> {
  const resp = await getChainGraph({ chainId, tradeDate });
  if (!resp.success || !resp.data) {
    return {
      nodes: [],
      edges: [],
      layers: [],
      summary: {
        chain_id: chainId,
        chain_name: chainId,
        hot_score: 0,
        market_strength: '暂无',
        top_node: '',
        top_node_name: '',
        limit_up_count: 0,
        turnover_change: 0,
        node_count: 0,
        stock_count: 0,
      },
      source: resp.meta?.source,
      is_fallback: Boolean(resp.meta?.demo_mode),
      data_date: resp.meta?.data_date,
      has_data: Boolean(resp.meta?.has_data),
      demo_mode: Boolean(resp.meta?.demo_mode),
      missing_sources: resp.meta?.missing_sources ?? [],
    };
  }
  return {
    nodes: resp.data.nodes,
    edges: resp.data.links,
    layers: resp.data.layers ?? [],
    summary: resp.data.summary ?? {
      chain_id: chainId,
      chain_name: chainId,
      hot_score: 0,
      market_strength: '暂无',
      top_node: '',
      top_node_name: '',
      limit_up_count: 0,
      turnover_change: 0,
      node_count: 0,
      stock_count: 0,
    },
    source: resp.data.source,
    is_fallback: Boolean(resp.data.demo_mode),
    data_date: resp.data.data_date,
    has_data: resp.data.has_data,
    demo_mode: resp.data.demo_mode,
    missing_sources: resp.data.missing_sources ?? [],
  };
}

export async function getNodeDetail(chainId: string, nodeId: string): Promise<NodeDetail> {
  const response = await service.get<ApiResponse<NodeDetail>>(
    `/api/industry-chain/node/${encodeURIComponent(nodeId)}`,
    { params: { chain_id: chainId } }
  );
  return response.data.data;
}

export async function getNodeStocks(
  chainId: string,
  nodeId: string,
  options?: {
    include_candidates?: boolean;
    verified_only?: boolean;
    sort_by?: string;
  }
): Promise<NodeStocksResponse> {
  const params: Record<string, string> = { chain_id: chainId };
  if (options?.include_candidates !== undefined) params.include_candidates = String(options.include_candidates);
  if (options?.verified_only !== undefined) params.verified_only = String(options.verified_only);
  if (options?.sort_by) params.sort_by = options.sort_by;
  const response = await service.get<ApiResponse<NodeStocksResponse>>(
    `/api/industry-chain/node/${encodeURIComponent(nodeId)}/stocks`,
    { params }
  );
  return response.data.data;
}

export async function searchIndustryChain(q: string): Promise<SearchResult> {
  const response = await service.get<ApiResponse<SearchResult>>(
    '/api/industry-chain/search',
    { params: { q } }
  );
  return response.data.data;
}

export async function getDataSourcesStatus(): Promise<DataSourceStatus> {
  const response = await service.get<ApiResponse<DataSourceStatus>>(
    '/api/industry-chain/data-sources/status'
  );
  return response.data.data;
}

export async function getIndustryChainDebug(): Promise<IndustryChainDebug> {
  const response = await service.get<ApiResponse<IndustryChainDebug>>('/api/industry-chain/debug');
  return response.data.data;
}

export async function triggerSync(chainId?: string, tradeDate?: string): Promise<Record<string, any>> {
  const params: Record<string, string> = {};
  if (chainId) params.chain_id = chainId;
  if (tradeDate) params.trade_date = tradeDate;
  const response = await service.post<ApiResponse<Record<string, any>>>(
    '/api/industry-chain/sync',
    null,
    { params }
  );
  return response.data.data;
}

/**
 * 工具：判断 data_date 是否 stale（与 today 相差 > 1 个交易日）。
 *  - 1 个交易日粗略按 3 个自然日估算（兼容周末）。
 */
export function isStale(dataDate: string | null | undefined): boolean {
  if (!dataDate) return false;
  const trimmed = String(dataDate).slice(0, 10);
  const target = new Date(trimmed);
  if (Number.isNaN(target.getTime())) return false;
  const now = new Date();
  const diff = now.getTime() - target.getTime();
  return diff > 3 * 24 * 60 * 60 * 1000;
}
