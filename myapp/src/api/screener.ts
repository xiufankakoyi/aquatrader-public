/**
 * 股票筛选器 API
 */
import service from './index'

export interface Indicator {
  field: string
  name: string
  type: 'number' | 'text' | 'boolean' | 'date'
  unit?: string
}

export interface IndicatorCategory {
  name: string
  indicators: Indicator[]
}

export interface Operator {
  value: string
  label: string
  input: 'single' | 'range' | 'percent' | 'boolean'
}

export interface FilterCondition {
  field: string
  operator: string
  value: any
  value2?: any
}

export interface OrderBy {
  field: string
  direction: 'asc' | 'desc'
}

export interface FilterRequest {
  date?: string
  conditions: FilterCondition[]
  logic?: 'AND' | 'OR'
  order_by?: OrderBy[]
  page?: number
  page_size?: number
  fields?: string[]
}

export interface FilterResponse {
  total: number
  page: number
  page_size: number
  total_pages: number
  date: string
  items?: any[]
  records?: any[]
}

export interface FieldStats {
  field: string
  date: string
  min: number
  max: number
  avg: number
  count: number
  non_null_count: number
}

/**
 * 获取指标列表
 */
export function getIndicators() {
  return service.get<{
    success: boolean
    data: {
      categories: Record<string, IndicatorCategory>
      operators: Record<string, Operator[]>
    }
  }>('/api/screener/indicators')
}

/**
 * 获取交易日期列表
 */
export function getTradeDates() {
  return service.get<{
    success: boolean
    data: {
      dates: string[]
      latest: string
    }
  }>('/api/screener/dates')
}

/**
 * 筛选股票
 */
export function filterStocks(params: FilterRequest) {
  return service.post<{
    success: boolean
    data: FilterResponse
    error?: string
  }>('/api/screener/filter', params)
}

/**
 * 获取字段统计信息
 */
export function getFieldStats(field: string, date?: string) {
  return service.post<{
    success: boolean
    data: FieldStats
    error?: string
  }>('/api/screener/field_stats', { field, date })
}

/**
 * 导出筛选结果
 */
export function exportStocks(params: FilterRequest) {
  return service.post('/api/screener/export', params, {
    responseType: 'blob'
  })
}
