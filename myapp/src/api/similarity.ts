/**
 * K线形态相似度 API
 */
import service from './index'

/** 匹配请求参数 */
export interface MatchRequest {
  stock_code: string
  window_size: number
  top_n: number
  pattern_type: string | null
  corr_threshold: number
  subsequent_days: number
  algorithm?: 'dtw' | 'skeleton'
  scene?: string
}

/** K线数据 */
export interface SubsequentKline {
  trade_date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

/** 匹配结果项 */
export interface MatchResultItem {
  stock_code: string
  stock_name?: string
  start_date: string
  end_date: string
  similarity_score: number
  structure_score?: number
  rhythm_score?: number
  ma_fit_score?: number
  enhanced_score?: number
  corr_score?: number
  subsequent_kline: SubsequentKline[]
  matched_kline: SubsequentKline[]
  preceding_kline: SubsequentKline[]
}

/** 预处理结果 */
export interface PreprocessResult {
  window_size: number
  total_windows: number
  symbols_count: number
}

/** 状态信息 */
export interface StatusResult {
  preprocessed: boolean
  window_sizes: number[]
  cache_sizes: Record<string, number>
  last_preprocess_time: string | null
}

/**
 * 匹配查询
 */
export function matchSimilarity(params: MatchRequest) {
  return service.post<{
    success: boolean
    data: MatchResultItem[]
    error?: string
  }>('/api/similarity/match', params)
}

/**
 * 触发预处理
 */
export function preprocessSimilarity(params: { window_size: number }) {
  return service.post<{
    success: boolean
    data: PreprocessResult
    error?: string
  }>('/api/similarity/preprocess', params)
}

/**
 * 获取预处理状态
 */
export function getSimilarityStatus() {
  return service.get<{
    success: boolean
    data: StatusResult
    error?: string
  }>('/api/similarity/status')
}

/** 股票搜索结果 */
export interface StockSearchResult {
  code: string
  name: string
}

/** K线数据项 */
export interface KlineItem {
  trade_date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

/**
 * 搜索股票代码
 */
export function searchStocks(keyword: string, limit: number = 10) {
  return service.get<{
    success: boolean
    data: StockSearchResult[]
    error?: string
  }>('/api/similarity/stocks/search', {
    params: { keyword, limit }
  })
}

/**
 * 获取股票K线数据
 */
export function getStockKline(stockCode: string, days: number = 30) {
  return service.get<{
    success: boolean
    data: {
      stock_code: string
      klines: KlineItem[]
    }
    error?: string
  }>(`/api/similarity/stocks/${stockCode}/kline`, {
    params: { days }
  })
}
