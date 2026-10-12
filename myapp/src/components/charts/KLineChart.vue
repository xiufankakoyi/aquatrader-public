<template>
  <div class="kline-chart-container">
    <!-- 工具栏 -->
    <div class="chart-toolbar">
      <div class="toolbar-left">
        <span class="symbol-label">{{ symbol }}</span>
        <span class="timeframe-label">{{ timeframe }}</span>
        <span v-if="isFromCache" class="cache-badge">
          <i class="fas fa-database"></i> 缓存
        </span>
      </div>
      <div class="toolbar-right">
        <button
          class="toolbar-btn"
          :class="{ active: showMA }"
          @click="showMA = !showMA"
          title="显示/隐藏均线"
        >
          MA
        </button>
        <button
          class="toolbar-btn"
          :class="{ active: showVolume }"
          @click="showVolume = !showVolume"
          title="显示/隐藏成交量"
        >
          VOL
        </button>
        <button
          class="toolbar-btn refresh-btn"
          :class="{ loading: isLoading }"
          @click="handleForceRefresh"
          title="强制刷新"
        >
          <i class="fas fa-sync-alt" :class="{ 'fa-spin': isLoading }"></i>
        </button>
      </div>
    </div>

    <!-- 图表区域 -->
    <div class="chart-wrapper">
      <div ref="chartContainer" class="chart-container"></div>

      <!-- 自定义图例 -->
      <div v-if="showLegend && legendData" class="custom-legend">
        <div class="legend-row">
          <span class="legend-item">
            <span class="legend-label">开</span>
            <span
              class="legend-value"
              :class="getPriceChangeClass(legendData.open, legendData.close)"
            >
              {{ formatPrice(legendData.open) }}
            </span>
          </span>
          <span class="legend-item">
            <span class="legend-label">高</span>
            <span
              class="legend-value"
              :class="getPriceChangeClass(legendData.high, legendData.close)"
            >
              {{ formatPrice(legendData.high) }}
            </span>
          </span>
          <span class="legend-item">
            <span class="legend-label">低</span>
            <span
              class="legend-value"
              :class="getPriceChangeClass(legendData.low, legendData.close)"
            >
              {{ formatPrice(legendData.low) }}
            </span>
          </span>
          <span class="legend-item">
            <span class="legend-label">收</span>
            <span
              class="legend-value"
              :class="getPriceChangeClass(legendData.close, legendData.open)"
            >
              {{ formatPrice(legendData.close) }}
            </span>
          </span>
        </div>
        <div v-if="showMA && (legendData.ma5 !== null || legendData.ma10 !== null)" class="legend-row ma-row">
          <span v-if="legendData.ma5 !== null" class="legend-item">
            <span class="legend-dot" style="background: #fbbf24"></span>
            <span class="legend-label">MA5</span>
            <span class="legend-value">{{ formatPrice(legendData.ma5) }}</span>
          </span>
          <span v-if="legendData.ma10 !== null" class="legend-item">
            <span class="legend-dot" style="background: #3b82f6"></span>
            <span class="legend-label">MA10</span>
            <span class="legend-value">{{ formatPrice(legendData.ma10) }}</span>
          </span>
          <span v-if="legendData.volume !== null" class="legend-item volume-item">
            <span class="legend-label">成交量</span>
            <span class="legend-value">{{ formatVolume(legendData.volume) }}</span>
          </span>
        </div>
      </div>

      <!-- 加载状态 -->
      <div v-if="isLoading" class="loading-overlay">
        <div class="loading-content">
          <i class="fas fa-spinner fa-spin"></i>
          <span>加载中...</span>
        </div>
      </div>

      <!-- 错误提示 -->
      <div v-if="error" class="error-overlay">
        <div class="error-content">
          <i class="fas fa-exclamation-triangle"></i>
          <span>{{ error }}</span>
          <button class="retry-btn" @click="loadData">重试</button>
        </div>
      </div>
    </div>

    <!-- 状态栏 -->
    <div class="chart-statusbar">
      <span class="status-item">
        <i class="fas fa-chart-bar"></i>
        数据点: {{ data.length }}
      </span>
      <span class="status-item">
        <i class="fas fa-calendar"></i>
        {{ dateRangeDisplay }}
      </span>
      <span v-if="lastUpdateTime" class="status-item">
        <i class="fas fa-clock"></i>
        更新: {{ lastUpdateTime }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  ref,
  onMounted,
  onUnmounted,
  watch,
  nextTick,
  computed
} from 'vue'
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type UTCTimestamp,
  CandlestickSeries,
  HistogramSeries,
  LineSeries
} from 'lightweight-charts'
import dayjs from 'dayjs'
import { dataManager, type KlineDataPoint } from '../../utils/DataManager'

// v5 series types
type CandlestickSeriesApi = ISeriesApi<'Candlestick', UTCTimestamp>
type HistogramSeriesApi = ISeriesApi<'Histogram', UTCTimestamp>
type LineSeriesApi = ISeriesApi<'Line', UTCTimestamp>

/**
 * KLineChart - 智能缓存K线图组件
 *
 * 特性：
 * 1. 集成 DataManager 智能缓存层
 * 2. 自动管理数据加载和缓存
 * 3. 支持强制刷新
 * 4. 显示缓存状态
 */

interface Props {
  /** 股票代码 */
  symbol: string
  /** 时间周期: 1d, 1h, 15m 等 */
  timeframe?: string
  /** 开始日期 */
  startDate: string
  /** 结束日期 */
  endDate: string
  /** 是否显示图例 */
  showLegend?: boolean
  /** 是否自动加载 */
  autoLoad?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  timeframe: '1d',
  showLegend: true,
  autoLoad: true
})

const emit = defineEmits<{
  hover: [
    data: {
      date: string
      open: number
      high: number
      low: number
      close: number
      volume?: number
    }
  ]
  click: [data: { date: string; price: number }]
  dataLoaded: [data: { count: number; fromCache: boolean }]
  error: [error: string]
}>()

// DOM 引用
const chartContainer = ref<HTMLDivElement | null>(null)

// 图表实例
let chart: IChartApi | null = null
let candlestickSeries: CandlestickSeriesApi | null = null
let volumeSeries: HistogramSeriesApi | null = null
let ma5Series: LineSeriesApi | null = null
let ma10Series: LineSeriesApi | null = null

// 状态
const data = ref<KlineDataPoint[]>([])
const isLoading = ref(false)
const isFromCache = ref(false)
const isLoadingMore = ref(false)
const hasMoreHistory = ref(true)
const error = ref<string | null>(null)
const lastUpdateTime = ref<string>('')
const showMA = ref(true)
const showVolume = ref(true)

// 图例数据
const legendData = ref<{
  open: number
  high: number
  low: number
  close: number
  ma5: number | null
  ma10: number | null
  volume: number | null
} | null>(null)

// A股颜色：红涨绿跌
const UP_COLOR = '#ef4444'
const DOWN_COLOR = '#22c55e'

// 计算属性
const dateRangeDisplay = computed(() => {
  if (data.value.length === 0) return '--'
  const start = data.value[0].date
  const end = data.value[data.value.length - 1].date
  return `${start} ~ ${end}`
})

/**
 * 格式化价格
 */
function formatPrice(price: number): string {
  if (price === undefined || price === null) return '--'
  return price.toFixed(2)
}

/**
 * 格式化成交量
 */
function formatVolume(volume: number): string {
  if (volume >= 100000000) {
    return (volume / 100000000).toFixed(2) + '亿'
  } else if (volume >= 10000) {
    return (volume / 10000).toFixed(2) + '万'
  }
  return volume.toFixed(0)
}

/**
 * 获取价格变化颜色类
 */
function getPriceChangeClass(current: number, base: number): string {
  if (current > base) return 'up'
  if (current < base) return 'down'
  return 'neutral'
}

/**
 * 将日期字符串转换为 UTC 时间戳
 */
function dateToTimestamp(dateStr: string): UTCTimestamp {
  const date = new Date(dateStr)
  return Math.floor(date.getTime() / 1000) as UTCTimestamp
}

/**
 * 加载数据（智能缓存入口）- 支持流式更新
 */
async function loadData(forceRefresh = false) {
  if (isLoading.value) return

  isLoading.value = true
  error.value = null
  isFromCache.value = false

  try {
    const startTime = performance.now()

    // 先尝试从缓存读取，立即显示
    const cachedData = await dataManager.getCache?.(props.symbol, props.timeframe)
    const hasCache = cachedData && cachedData.data.length > 0 && !forceRefresh

    if (hasCache) {
      data.value = cachedData.data
      isFromCache.value = true
      lastUpdateTime.value = new Date().toLocaleTimeString()
      nextTick(() => updateChartData())
      console.log(`[KLineChart] 缓存数据立即显示: ${cachedData.data.length} 条`)
    }

    // 后台加载最新数据
    const result = await dataManager.getKlineData({
      symbol: props.symbol,
      timeframe: props.timeframe,
      startDate: props.startDate,
      endDate: props.endDate,
      forceRefresh
    })

    const loadTime = performance.now() - startTime

    // 更新图表数据（无论是否有缓存都更新，确保数据最新）
    if (result.length > 0) {
      data.value = result
      lastUpdateTime.value = new Date().toLocaleTimeString()
      // 初始加载时假设还有更早的数据，后续根据实际情况更新
      hasMoreHistory.value = true
      nextTick(() => updateChartData())
    }

    emit('dataLoaded', {
      count: result.length,
      fromCache: isFromCache.value
    })

    console.log(
      `[KLineChart] 数据加载完成: ${result.length} 条, ` +
        `${isFromCache.value ? '来自缓存' : '来自服务器'}, ` +
        `耗时: ${loadTime.toFixed(2)}ms`
    )
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : '数据加载失败'
    error.value = errorMsg
    emit('error', errorMsg)
    console.error('[KLineChart] 数据加载失败:', err)
  } finally {
    isLoading.value = false
  }
}

/**
 * 强制刷新
 */
function handleForceRefresh() {
  loadData(true)
}

/**
 * 加载更多历史数据（流式加载）
 */
async function loadMoreHistory() {
  if (isLoadingMore.value || !hasMoreHistory.value) return

  isLoadingMore.value = true
  error.value = null

  try {
    // 找到已加载数据中的最早日期
    const earliestDate = data.value.reduce(
      (min, d) => (d.date < min ? d.date : min),
      data.value[0]?.date || props.startDate
    )

    // 计算要加载的历史范围：往前推60天
    const newStartDate = dayjs(earliestDate).subtract(60, 'day').format('YYYY-MM-DD')
    const newEndDate = dayjs(earliestDate).subtract(1, 'day').format('YYYY-MM-DD')

    console.log(`[KLineChart] 加载历史: ${newStartDate} ~ ${newEndDate}`)

    // 从服务器获取更早的数据
    const moreData = await dataManager.fetchFromServerPublic(
      props.symbol,
      props.timeframe,
      newStartDate,
      newEndDate
    )

    if (moreData.length === 0) {
      hasMoreHistory.value = false
      return
    }

    // 合并数据并去重
    const existingDates = new Set(data.value.map(d => d.date))
    const newPoints = moreData.filter(d => !existingDates.has(d.date))

    if (newPoints.length > 0) {
      // 记录当前可见范围的起始位置（用于保持用户视角）
      let currentVisibleFrom = 0
      if (chart) {
        const range = chart.timeScale().getVisibleLogicalRange()
        if (range) currentVisibleFrom = range.from
      }

      data.value = [...newPoints, ...data.value].sort((a, b) =>
        dateToTimestamp(a.date) - dateToTimestamp(b.date)
      )

      // 更新缓存
      dataManager.setCache(props.symbol, props.timeframe, data.value)

      console.log(`[KLineChart] 历史加载完成: 新增${newPoints.length}条, 共${data.value.length}条`)

      if (newPoints.length < 60) {
        hasMoreHistory.value = false
      }

      // 更新图表并调整可见范围以保持用户视角
      nextTick(() => {
        updateChartData()
        // 保持用户之前看到的起始位置（加上新数据的偏移）
        if (chart && newPoints.length > 0) {
          chart.timeScale().setVisibleLogicalRange({
            from: currentVisibleFrom + newPoints.length,
            to: currentVisibleFrom + newPoints.length + (data.value.length - currentVisibleFrom - newPoints.length)
          })
        }
      })
    } else {
      hasMoreHistory.value = false
    }
  } catch (err) {
    console.error('[KLineChart] 加载历史失败:', err)
  } finally {
    isLoadingMore.value = false
  }
}

/**
 * 初始化图表
 */
function initChart() {
  if (!chartContainer.value) return

  // 销毁旧图表
  if (chart) {
    chart.remove()
    chart = null
  }

  // 创建图表
  chart = createChart(chartContainer.value, {
    layout: {
      background: { color: 'transparent' },
      textColor: '#d1d4dc',
      fontFamily: "'JetBrains Mono', 'Microsoft YaHei', monospace"
    },
    grid: {
      vertLines: { color: 'rgba(42, 46, 57, 0.5)' },
      horzLines: { color: 'rgba(42, 46, 57, 0.5)' }
    },
    crosshair: {
      mode: 0
    },
    rightPriceScale: {
      borderColor: '#2a2e39',
      scaleMargins: {
        top: 0.1,
        bottom: 0.2
      }
    },
    timeScale: {
      borderColor: '#2a2e39',
      timeVisible: false,
      secondsVisible: false,
      rightOffset: 0, // 右侧不留空，最新K线贴边
      barSpacing: 8, // K线宽度
      lockVisibleTimeRangeOnResize: true,
      tickMarkFormatter: (time: number) => {
        const date = new Date(time * 1000)
        return `${date.getMonth() + 1}/${date.getDate()}`
      }
    },
    handleScroll: {
      vertTouchDrag: false,
      horzTouchDrag: true,
      mouseWheel: true,
      pressedMouseMove: true
    },
    handleScale: {
      axisPressedMouseMove: {
        time: true,
        price: false
      },
      mouseWheel: true,
      pinch: true
    }
  })

  // 创建K线系列
  candlestickSeries = chart.addSeries(CandlestickSeries, {
    upColor: UP_COLOR,
    downColor: DOWN_COLOR,
    borderUpColor: UP_COLOR,
    borderDownColor: DOWN_COLOR,
    wickUpColor: UP_COLOR,
    wickDownColor: DOWN_COLOR,
    priceFormat: {
      type: 'price',
      precision: 2,
      minMove: 0.01
    }
  })

  // 创建成交量系列 - 使用独立的 overlay 价格轴
  volumeSeries = chart.addSeries(HistogramSeries, {
    color: '#26a69a',
    priceFormat: {
      type: 'volume'
    },
    priceScaleId: 'volume',
    priceLineVisible: false
  })

  // 配置成交量价格轴的边距，压缩成交量显示区域
  chart.priceScale('volume').applyOptions({
    scaleMargins: {
      top: 0.92,
      bottom: 0
    }
  })

  // 创建MA线
  ma5Series = chart.addSeries(LineSeries, {
    color: '#fbbf24',
    lineWidth: 2,
    title: 'MA5',
    lastValueVisible: false,
    priceLineVisible: false
  })

  ma10Series = chart.addSeries(LineSeries, {
    color: '#3b82f6',
    lineWidth: 2,
    title: 'MA10',
    lastValueVisible: false,
    priceLineVisible: false
  })

  // 监听十字光标移动
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point || !candlestickSeries) {
      return
    }

    const seriesData = param.seriesData.get(candlestickSeries)
    if (seriesData) {
      const candleData = seriesData as CandlestickData
      const timestamp = param.time as number
      const date = new Date(timestamp * 1000).toISOString().split('T')[0]

      // 查找对应的原始数据
      const originalData = data.value.find((d) => d.date === date)

      legendData.value = {
        open: candleData.open,
        high: candleData.high,
        low: candleData.low,
        close: candleData.close,
        ma5: originalData ? calculateMAAtIndex(date, 5) : null,
        ma10: originalData ? calculateMAAtIndex(date, 10) : null,
        volume: originalData?.volume ?? null
      }

      emit('hover', {
        date,
        open: candleData.open,
        high: candleData.high,
        low: candleData.low,
        close: candleData.close,
        volume: originalData?.volume
      })
    }
  })

  // 监听点击事件
  chart.subscribeClick((param) => {
    if (param.time && param.point && candlestickSeries) {
      const timestamp = param.time as number
      const date = new Date(timestamp * 1000).toISOString().split('T')[0]
      const price = candlestickSeries.coordinateToPrice(param.point.y) || 0

      emit('click', { date, price })
    }
  })

  // 监听可见范围变化
  chart.timeScale().subscribeVisibleLogicalRangeChange((visibleRange) => {
    if (!chart || !visibleRange || data.value.length === 0 || isLoadingMore.value) return

    const lastIndex = data.value.length - 1
    const rightEdge = lastIndex + 0.5

    // 简单限制右边不超过最新数据
    if (visibleRange.to > rightEdge + 3) {
      const barsCount = visibleRange.to - visibleRange.from
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, rightEdge - barsCount),
        to: rightEdge
      })
      return
    }

    // 左边边界检测：触发历史数据加载
    if (visibleRange.from <= 2 && hasMoreHistory.value) {
      console.log('[KLineChart] 检测到左边界，开始加载历史')
      loadMoreHistory()
    }
  })

  // 适应容器大小
  handleResize()
}

/**
 * 计算指定日期的MA值
 */
function calculateMAAtIndex(date: string, period: number): number | null {
  const index = data.value.findIndex((d) => d.date === date)
  if (index < period - 1) return null

  let sum = 0
  for (let i = 0; i < period; i++) {
    sum += data.value[index - i].close
  }
  return sum / period
}

/**
 * 计算MA数据
 */
function calculateMA(period: number, sourceData: KlineDataPoint[]): LineData[] {
  const result: LineData[] = []
  for (let i = 0; i < sourceData.length; i++) {
    if (i < period - 1) continue

    let sum = 0
    for (let j = 0; j < period; j++) {
      sum += sourceData[i - j].close
    }
    result.push({
      time: dateToTimestamp(sourceData[i].date),
      value: sum / period
    })
  }
  return result
}

/**
 * 更新图表数据
 */
function updateChartData() {
  if (!candlestickSeries || data.value.length === 0) return

  const sortedData = [...data.value].sort((a, b) =>
    dateToTimestamp(a.date) - dateToTimestamp(b.date)
  )

  // 更新K线数据
  const candleData: CandlestickData[] = sortedData.map((item) => ({
    time: dateToTimestamp(item.date),
    open: item.open,
    high: item.high,
    low: item.low,
    close: item.close
  }))
  candlestickSeries.setData(candleData)

  // 更新成交量
  if (volumeSeries && showVolume.value) {
    const volumeData: HistogramData[] = sortedData.map((item) => ({
      time: dateToTimestamp(item.date),
      value: item.volume || 0,
      color: item.close >= item.open ? UP_COLOR : DOWN_COLOR
    }))
    volumeSeries.setData(volumeData)
    volumeSeries.applyOptions({ visible: true })
  } else if (volumeSeries) {
    volumeSeries.applyOptions({ visible: false })
  }

  // 更新MA线
  if (ma5Series && ma10Series && showMA.value) {
    const ma5Data = calculateMA(5, sortedData)
    const ma10Data = calculateMA(10, sortedData)
    ma5Series.setData(ma5Data)
    ma10Series.setData(ma10Data)
    ma5Series.applyOptions({ visible: true })
    ma10Series.applyOptions({ visible: true })
  } else if (ma5Series && ma10Series) {
    ma5Series.applyOptions({ visible: false })
    ma10Series.applyOptions({ visible: false })
  }

  // 右对齐：显示最近的数据，最新K线贴边
  if (chart && candleData.length > 0) {
    const timeScale = chart.timeScale()
    const lastIndex = candleData.length - 1
    // 默认显示60根K线，如果数据不够就显示全部
    const visibleCount = Math.min(60, candleData.length)
    timeScale.setVisibleLogicalRange({
      from: lastIndex - visibleCount + 1,
      to: lastIndex + 0.5
    })
  }
}

/**
 * 处理窗口大小变化
 */
function handleResize() {
  if (chart && chartContainer.value) {
    const { width, height } = chartContainer.value.getBoundingClientRect()
    chart.applyOptions({ width, height })
  }
}

/**
 * 设置可见范围
 */
function setVisibleRange(from: string, to: string) {
  if (!chart) return
  chart.timeScale().setVisibleRange({
    from: dateToTimestamp(from) as UTCTimestamp,
    to: dateToTimestamp(to) as UTCTimestamp
  })
}

/**
 * 跳转到指定日期
 */
function scrollToDate(date: string) {
  if (!chart) return
  chart.timeScale().scrollToPosition(dateToTimestamp(date), true)
}

// 监听显示选项变化
watch(showMA, () => {
  nextTick(() => updateChartData())
})

watch(showVolume, () => {
  nextTick(() => updateChartData())
})

// 监听参数变化，自动重新加载
watch(
  () => [props.symbol, props.timeframe, props.startDate, props.endDate],
  () => {
    if (props.autoLoad) {
      loadData()
    }
  },
  { deep: true }
)

// 生命周期
onMounted(() => {
  nextTick(() => {
    initChart()
    window.addEventListener('resize', handleResize)

    if (props.autoLoad) {
      loadData()
    }
  })
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (chart) {
    chart.remove()
    chart = null
  }
})

// 暴露方法
defineExpose({
  loadData,
  handleForceRefresh,
  setVisibleRange,
  scrollToDate,
  handleResize,
  clearCache: () => dataManager.clearCache(props.symbol, props.timeframe)
})
</script>

<style scoped>
.kline-chart-container {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 400px;
  background: #0A0A0A;
  border-radius: 8px;
  overflow: hidden;
}

/* 工具栏 */
.chart-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #1e222d;
  border-bottom: 1px solid #2a2e39;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.symbol-label {
  font-size: 14px;
  font-weight: 600;
  color: #d1d4dc;
}

.timeframe-label {
  font-size: 12px;
  color: #787b86;
  padding: 2px 6px;
  background: #2a2e39;
  border-radius: 4px;
}

.cache-badge {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: #22c55e;
  padding: 2px 6px;
  background: rgba(34, 197, 94, 0.1);
  border-radius: 4px;
}

.cache-badge i {
  font-size: 10px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-btn {
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 500;
  color: #787b86;
  background: #2a2e39;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.toolbar-btn:hover {
  background: #363a45;
  color: #d1d4dc;
}

.toolbar-btn.active {
  background: #2962ff;
  color: #fff;
}

.refresh-btn {
  padding: 4px 8px;
}

.refresh-btn.loading {
  opacity: 0.6;
  cursor: not-allowed;
}

/* 图表区域 */
.chart-wrapper {
  position: relative;
  flex: 1;
  min-height: 0;
}

.chart-container {
  width: 100%;
  height: 100%;
  min-height: 380px;
}

/* 自定义图例 */
.custom-legend {
  position: absolute;
  top: 8px;
  left: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  pointer-events: none;
  z-index: 10;
}

.legend-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ma-row {
  margin-top: 2px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
}

.legend-label {
  color: #787b86;
  font-weight: 500;
}

.legend-value {
  font-weight: 600;
  color: #d1d4dc;
}

.legend-value.up {
  color: #ef4444;
}

.legend-value.down {
  color: #22c55e;
}

.legend-value.neutral {
  color: #d1d4dc;
}

.legend-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.volume-item {
  margin-left: 8px;
  padding-left: 8px;
  border-left: 1px solid #2a2e39;
}

/* 加载状态 */
.loading-overlay {
  position: absolute;
  inset: 0;
  background: rgba(19, 23, 34, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}

.loading-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: #787b86;
  font-size: 12px;
}

.loading-content i {
  font-size: 20px;
  color: #2962ff;
}

/* 错误状态 */
.error-overlay {
  position: absolute;
  inset: 0;
  background: rgba(19, 23, 34, 0.9);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}

.error-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: #ef4444;
  font-size: 13px;
}

.error-content i {
  font-size: 24px;
}

.retry-btn {
  padding: 6px 16px;
  font-size: 12px;
  color: #fff;
  background: #ef4444;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.2s;
}

.retry-btn:hover {
  background: #dc2626;
}

/* 状态栏 */
.chart-statusbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 6px 12px;
  background: #1e222d;
  border-top: 1px solid #2a2e39;
  font-size: 11px;
  color: #787b86;
}

.status-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.status-item i {
  font-size: 10px;
}
</style>
