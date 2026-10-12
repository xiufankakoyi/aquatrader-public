import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type {
  Indicator,
  IndicatorCategory,
  Operator,
  FilterCondition,
  OrderBy,
  FilterResponse
} from '@/api/screener'
import {
  getIndicators,
  getTradeDates,
  filterStocks,
  getFieldStats,
  exportStocks
} from '@/api/screener'
import { message } from 'ant-design-vue'

export interface FilterTemplate {
  id: string
  name: string
  conditions: FilterCondition[]
  logic: 'AND' | 'OR'
  orderBy: OrderBy[]
  createdAt: string
}

export const useScreenerStore = defineStore('screener', () => {
  // ============ State ============
  const categories = ref<Record<string, IndicatorCategory>>({})
  const operators = ref<Record<string, Operator[]>>({})
  const tradeDates = ref<string[]>([])
  const latestDate = ref<string>('')
  const selectedDate = ref<string>('')
  const conditions = ref<FilterCondition[]>([])
  const logic = ref<'AND' | 'OR'>('AND')
  const orderBy = ref<OrderBy[]>([])
  const filterResult = ref<FilterResponse | null>(null)
  const allRecords = ref<any[]>([])
  const loading = ref(false)
  const error = ref('')
  const fieldStats = ref<Record<string, any>>({})
  const templates = ref<FilterTemplate[]>([])
  const currentPage = ref(1)
  const pageSize = ref(20)

  // ============ Getters ============
  const allIndicators = computed(() => {
    const result: Indicator[] = []
    Object.values(categories.value).forEach(cat => {
      result.push(...cat.indicators)
    })
    return result
  })

  const indicatorsByCategory = computed(() => categories.value)

  const hasConditions = computed(() => conditions.value.length > 0)

  const totalStocks = computed(() => filterResult.value?.total || 0)

  const paginatedRecords = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value
    const end = start + pageSize.value
    return allRecords.value.slice(start, end)
  })

  // ============ Actions ============

  /**
   * 加载指标列表
   */
  async function loadIndicators() {
    error.value = ''
    try {
      const res = await getIndicators()
      if (res.data.success) {
        categories.value = res.data.data.categories
        operators.value = res.data.data.operators
      }
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '加载指标列表失败'
      message.error('加载指标列表失败')
      console.error(caught)
    }
  }

  /**
   * 加载交易日期
   */
  async function loadTradeDates() {
    error.value = ''
    try {
      const res = await getTradeDates()
      if (res.data.success) {
        tradeDates.value = res.data.data.dates
        latestDate.value = res.data.data.latest
        if (!selectedDate.value) {
          selectedDate.value = latestDate.value
        }
      }
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '加载交易日期失败'
      message.error('加载交易日期失败')
      console.error(caught)
    }
  }

  /**
   * 添加筛选条件
   */
  function addCondition(condition: FilterCondition) {
    conditions.value.push(condition)
  }

  /**
   * 更新筛选条件
   */
  function updateCondition(index: number, condition: FilterCondition) {
    if (index >= 0 && index < conditions.value.length) {
      conditions.value[index] = condition
    }
  }

  /**
   * 删除筛选条件
   */
  function removeCondition(index: number) {
    conditions.value.splice(index, 1)
  }

  /**
   * 清空所有条件
   */
  function clearConditions() {
    conditions.value = []
  }

  /**
   * 设置逻辑关系
   */
  function setLogic(newLogic: 'AND' | 'OR') {
    logic.value = newLogic
  }

  /**
   * 设置排序
   */
  function setOrderBy(newOrderBy: OrderBy[]) {
    orderBy.value = newOrderBy
  }

  /**
   * 添加排序
   */
  function addOrderBy(field: string, direction: 'asc' | 'desc' = 'desc') {
    // 检查是否已存在
    const existingIndex = orderBy.value.findIndex(o => o.field === field)
    if (existingIndex >= 0) {
      orderBy.value[existingIndex].direction = direction
    } else {
      orderBy.value.push({ field, direction })
    }
  }

  /**
   * 移除排序
   */
  function removeOrderBy(field: string) {
    const index = orderBy.value.findIndex(o => o.field === field)
    if (index >= 0) {
      orderBy.value.splice(index, 1)
    }
  }

  /**
   * 执行筛选
   */
  async function executeFilter() {
    // 确保有选择日期
    const date = selectedDate.value || latestDate.value
    if (!date) {
      message.warning('请先选择交易日期')
      return
    }

    loading.value = true
    error.value = ''
    try {
      const res = await filterStocks({
        date: date,
        conditions: conditions.value,
        logic: logic.value,
        order_by: orderBy.value,
        page: 1,
        page_size: 10000
      })

      if (res.data.success) {
        filterResult.value = res.data.data
        // 兼容后端 items 命名（当前实现）与旧 records 别名（潜在调用方）
        allRecords.value = res.data.data.items || res.data.data.records || []
        currentPage.value = 1
        message.success(`筛选完成，共找到 ${res.data.data.total} 只股票`)
      } else {
        message.error(res.data.error || '筛选失败')
      }
    } catch (caught: any) {
      error.value = caught.response?.status === 404
        ? `该日期暂无数据: ${date}`
        : (caught.message || '筛选请求失败')
      if (caught.response?.status === 404) {
        message.error(`该日期暂无数据: ${date}`)
      } else {
        message.error('筛选请求失败')
        console.error(caught)
      }
    } finally {
      loading.value = false
    }
  }

  /**
   * 获取字段统计
   */
  async function loadFieldStats(field: string) {
    try {
      // 如果没有选择日期，使用最新日期
      const date = selectedDate.value || latestDate.value
      if (!date) {
        console.warn('No date available for field stats')
        return
      }

      const res = await getFieldStats(field, date)
      if (res.data.success) {
        fieldStats.value[field] = res.data.data
      } else {
        console.warn('加载字段统计失败:', res.data.error)
      }
    } catch (error: any) {
      // 静默处理错误，避免控制台报错影响用户体验
      if (error.response?.status === 404) {
        console.warn('该日期暂无数据:', selectedDate.value)
      } else {
        console.error('加载字段统计失败:', error)
      }
    }
  }

  /**
   * 导出结果
   */
  async function exportResults() {
    try {
      const res = await exportStocks({
        date: selectedDate.value,
        conditions: conditions.value,
        logic: logic.value,
        order_by: orderBy.value
      })

      // 创建下载链接
      const blob = new Blob([res.data], { type: 'text/csv' })
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `stock_screener_${selectedDate.value}.csv`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.URL.revokeObjectURL(url)

      message.success('导出成功')
    } catch (error) {
      message.error('导出失败')
      console.error(error)
    }
  }

  /**
   * 保存模板
   */
  function saveTemplate(name: string) {
    const template: FilterTemplate = {
      id: Date.now().toString(),
      name,
      conditions: JSON.parse(JSON.stringify(conditions.value)),
      logic: logic.value,
      orderBy: JSON.parse(JSON.stringify(orderBy.value)),
      createdAt: new Date().toISOString()
    }
    templates.value.push(template)
    // 保存到 localStorage
    localStorage.setItem('screener_templates', JSON.stringify(templates.value))
    message.success('模板保存成功')
  }

  /**
   * 加载模板
   */
  function loadTemplate(templateId: string) {
    const template = templates.value.find(t => t.id === templateId)
    if (template) {
      conditions.value = JSON.parse(JSON.stringify(template.conditions))
      logic.value = template.logic
      orderBy.value = JSON.parse(JSON.stringify(template.orderBy))
      message.success('模板加载成功')
    }
  }

  /**
   * 删除模板
   */
  function deleteTemplate(templateId: string) {
    const index = templates.value.findIndex(t => t.id === templateId)
    if (index >= 0) {
      templates.value.splice(index, 1)
      localStorage.setItem('screener_templates', JSON.stringify(templates.value))
      message.success('模板删除成功')
    }
  }

  /**
   * 从 localStorage 加载模板
   */
  function loadTemplatesFromStorage() {
    const stored = localStorage.getItem('screener_templates')
    if (stored) {
      try {
        templates.value = JSON.parse(stored)
      } catch (e) {
        console.error('加载模板失败:', e)
      }
    }
  }

  /**
   * 设置当前页
   */
  function setPage(page: number) {
    currentPage.value = page
  }

  /**
   * 设置每页数量
   */
  function setPageSize(size: number) {
    pageSize.value = size
    currentPage.value = 1
  }

  /**
   * 获取指标信息
   */
  function getIndicatorByField(field: string): Indicator | undefined {
    return allIndicators.value.find(ind => ind.field === field)
  }

  /**
   * 获取指标类型
   */
  function getIndicatorType(field: string): string {
    const indicator = getIndicatorByField(field)
    return indicator?.type || 'number'
  }

  /**
   * 获取指标名称
   */
  function getIndicatorName(field: string): string {
    const indicator = getIndicatorByField(field)
    return indicator?.name || field
  }

  /**
   * 获取指标单位
   */
  function getIndicatorUnit(field: string): string {
    const indicator = getIndicatorByField(field)
    return indicator?.unit || ''
  }

  return {
    // State
    categories,
    operators,
    tradeDates,
    latestDate,
    selectedDate,
    conditions,
    logic,
    orderBy,
    filterResult,
    allRecords,
    loading,
    error,
    fieldStats,
    templates,
    currentPage,
    pageSize,

    // Getters
    allIndicators,
    indicatorsByCategory,
    hasConditions,
    totalStocks,
    paginatedRecords,

    // Actions
    loadIndicators,
    loadTradeDates,
    addCondition,
    updateCondition,
    removeCondition,
    clearConditions,
    setLogic,
    setOrderBy,
    addOrderBy,
    removeOrderBy,
    executeFilter,
    loadFieldStats,
    exportResults,
    saveTemplate,
    loadTemplate,
    deleteTemplate,
    loadTemplatesFromStorage,
    setPage,
    setPageSize,
    getIndicatorByField,
    getIndicatorType,
    getIndicatorName,
    getIndicatorUnit
  }
})
