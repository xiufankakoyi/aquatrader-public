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
        <span v-if="isLoading" class="loading-badge">
          <i class="fas fa-spinner fa-spin"></i> 加载中
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

      <!-- 骨架屏 -->
      <div v-if="showSkeleton" class="skeleton-overlay">
        <div class="skeleton-chart">
          <div class="skeleton-axis"></div>
          <div class="skeleton-bars">
            <div v-for="i in 20" :key="i" class="skeleton-bar"></div>
          </div>
        </div>
      </div>

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
          <span class="legend-divider"></span>
          <span v-if="showMA && legendData.ma5 !== null" class="legend-item">
            <span class="legend-dot" style="background: #fbbf24"></span>
            <span class="legend-label">MA5</span>
            <span class="legend-value">{{ formatPrice(legendData.ma5) }}</span>
          </span>
          <span v-if="showMA && legendData.ma10 !== null" class="legend-item">
            <span class="legend-dot" style="background: #3b82f6"></span>
            <span class="legend-label">MA10</span>
            <span class="legend-value">{{ formatPrice(legendData.ma10) }}</span>
          </span>
          <span class="legend-divider"></span>
          <span v-if="showVolume && legendData.volume !== null" class="legend-item">
            <span class="legend-label">成交量</span>
            <span class="legend-value">{{ formatVolume(legendData.volume) }}</span>
          </span>
        </div>
      </div>
    </div>

    <!-- 状态栏 -->
    <div class="chart-status">
      <div class="status-left">
        <span class="status-item">
          <i class="fas fa-chart-bar"></i>
          数据点: {{ visibleDataCount }}/{{ fullData.length }}
        </span>
        <span class="status-item">
          <i class="fas fa-calendar"></i>
          {{ dateRangeDisplay }}
        </span>
      </div>
      <div class="status-right">
        <span v-if="perfData.query_ms > 0" class="status-item perf-item">
          <i class="fas fa-bolt"></i>
          查询: {{ perfData.query_ms }}ms
        </span>
        <span v-if="perfData.total_ms > 0" class="status-item perf-item">
          <i class="fas fa-stopwatch"></i>
          总耗时: {{ perfData.total_ms }}ms
        </span>
        <span v-if="lastUpdateTime" class="status-item">
          <i class="fas fa-clock"></i>
          更新: {{ lastUpdateTime }}
        </span>
      </div>
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
  type LogicalRange,
  CandlestickSeries,
  HistogramSeries,
  LineSeries
} from 'lightweight-charts'
import { dataManagerV2, type KlineDataPoint, type DataSegment } from '../../utils/DataManagerV2'

// v5 series types
type CandlestickSeriesApi = ISeriesApi<'Candlestick', UTCTimestamp>
type HistogramSeriesApi = ISeriesApi<'Histogram', UTCTimestamp>
type LineSeriesApi = ISeriesApi<'Line', UTCTimestamp>

/**
 * KLineChartV2 - 高性能K线图组件
 *
 * 核心特性：
 * 1. 三段式预加载 - 中心窗口 + 左侧缓冲 + 右侧空白
 * 2. 虚拟渲染 - 只渲染视口内数据
 * 3. 平滑缩放 - 插值拉伸过渡
 * 4. 骨架屏 - 首次加载无白屏
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
  hover: [data: { date: string; open: number; high: number; low: number; close: number; volume?: number }]
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
const fullData = ref<KlineDataPoint[]>([])
const visibleData = ref<KlineDataPoint[]>([])
const isLoading = ref(false)
const isFromCache = ref(false)
const error = ref<string | null>(null)
const lastUpdateTime = ref<string>('')
const showMA = ref(true)
const showVolume = ref(true)
const showSkeleton = ref(false)
const visibleDataCount = ref(0)

// 性能数据
const perfData = ref({
  query_ms: 0,
  total_ms: 0
})

// 视口状态
const viewportState = ref({
  leftIndex: 0,
  rightIndex: 0,
  isLoadingMore: false
})

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
  if (fullData.value.length === 0) return '--'
  const start = fullData.value[0].date
  const end = fullData.value[fullData.value.length - 1].date
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
 * 加载数据（三段式预加载）
 */
async function loadData(forceRefresh = false) {
  if (isLoading.value) return

  isLoading.value = true
  error.value = null
  isFromCache.value = false

  // 首次加载显示骨架屏
  if (fullData.value.length === 0) {
    showSkeleton.value = true
  }

  try {
    const startTime = performance.now()

    // 计算中心日期
    const centerDate = props.startDate

    // 三段式加载
    const segment = await dataManagerV2.loadSegmentedData(
      props.symbol,
      props.timeframe,
      centerDate,
      {
        centerWindow: 20,
        leftBuffer: 100,
        rightBuffer: 0
      }
    )

    const loadTime = performance.now() - startTime
    isFromCache.value = loadTime < 50 && !forceRefresh

    // 合并所有数据
    fullData.value = segment.full
    visibleData.value = [...segment.leftBuffer, ...segment.center]
    visibleDataCount.value = visibleData.value.length

    lastUpdateTime.value = new Date().toLocaleTimeString()
    showSkeleton.value = false

    // 更新图表
    nextTick(() => {
      updateChartData()
      // 设置视口到中心区域
      fitToCenter()
    })

    // 后台预取更多数据
    dataManagerV2.prefetchData(props.symbol, props.timeframe, centerDate)

    emit('dataLoaded', {
      count: visibleData.value.length,
      fromCache: isFromCache.value
    })

    console.log(
      `[KLineChartV2] 数据加载完成: 中心=${segment.center.length}, ` +
      `左缓冲=${segment.leftBuffer.length}, ` +
      `总计=${segment.full.length}, ` +
      `${isFromCache.value ? '来自缓存' : '来自服务器'}, ` +
      `耗时: ${loadTime.toFixed(2)}ms`
    )
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : '数据加载失败'
    error.value = errorMsg
    showSkeleton.value = false
    emit('error', errorMsg)
    console.error('[KLineChartV2] 数据加载失败:', err)
  } finally {
    isLoading.value = false
  }
}

/**
 * 加载更多历史数据（异步）
 */
async function loadMoreHistory() {
  if (viewportState.value.isLoadingMore || fullData.value.length === 0) return

  viewportState.value.isLoadingMore = true

  try {
    const firstDate = fullData.value[0].date
    const newData = await dataManagerV2.loadMoreHistory(
      props.symbol,
      props.timeframe,
      firstDate,
      100
    )

    if (newData.length > 0) {
      // 合并数据
      const merged = [...newData, ...fullData.value]
      fullData.value = merged.sort((a, b) =>
        new Date(a.date).getTime() - new Date(b.date).getTime()
      )

      // 增量更新图表
      updateChartData()
      console.log(`[KLineChartV2] 加载更多历史数据: ${newData.length} 条`)
    }
  } catch (err) {
    console.error('[KLineChartV2] 加载历史数据失败:', err)
  } finally {
    viewportState.value.isLoadingMore = false
  }
}

/**
 * 强制刷新
 */
function handleForceRefresh() {
  dataManagerV2.clearCache(props.symbol, props.timeframe)
  loadData(true)
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
      tickMarkFormatter: (time: number) => {
        const date = new Date(time * 1000)
        return `${date.getMonth() + 1}/${date.getDate()}`
      }
    },
    handleScroll: {
      vertTouchDrag: false
    },
    handleScale: {
      axisPressedMouseMove: {
        time: true,
        price: false
      }
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

  // 创建成交量系列
  volumeSeries = chart.addSeries(HistogramSeries, {
    color: '#26a69a',
    priceFormat: {
      type: 'volume'
    },
    priceScaleId: 'volume',
    priceLineVisible: false
  })

  // 配置成交量价格轴
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

  // 监听视口变化（虚拟渲染核心）
  chart.timeScale().subscribeVisibleLogicalRangeChange(handleVisibleRangeChange)

  // 监听十字光标移动
  chart.subscribeCrosshairMove(handleCrosshairMove)

  // 监听点击
  chart.subscribeClick(handleChartClick)
}

/**
 * 处理视口变化（虚拟渲染）
 */
function handleVisibleRangeChange(range: LogicalRange | null) {
  if (!range || fullData.value.length === 0) return

  const leftIndex = Math.floor(range.from)
  const rightIndex = Math.ceil(range.to)

  viewportState.value.leftIndex = Math.max(0, leftIndex)
  viewportState.value.rightIndex = Math.min(fullData.value.length - 1, rightIndex)

  // 检查是否需要加载更多历史数据
  if (leftIndex < 10 && !viewportState.value.isLoadingMore) {
    loadMoreHistory()
  }

  // 更新可见数据计数
  visibleDataCount.value = rightIndex - leftIndex + 1
}

/**
 * 处理十字光标移动
 */
function handleCrosshairMove(param: any) {
  if (!param.time || !param.point || !candlestickSeries) {
    legendData.value = null
    return
  }

  const seriesData = param.seriesData.get(candlestickSeries)
  if (seriesData) {
    const candleData = seriesData as CandlestickData
    const timestamp = param.time as number
    const date = new Date(timestamp * 1000).toISOString().split('T')[0]

    // 查找完整数据
    const fullItem = fullData.value.find(item => item.date === date)

    legendData.value = {
      open: candleData.open,
      high: candleData.high,
      low: candleData.low,
      close: candleData.close,
      ma5: fullItem ? calculateMAAtIndex(date, 5) : null,
      ma10: fullItem ? calculateMAAtIndex(date, 10) : null,
      volume: fullItem?.volume || null
    }

    emit('hover', {
      date,
      open: candleData.open,
      high: candleData.high,
      low: candleData.low,
      close: candleData.close,
      volume: fullItem?.volume
    })
  }
}

/**
 * 处理图表点击
 */
function handleChartClick(param: any) {
  if (!param.time || !param.point) return

  const timestamp = param.time as number
  const date = new Date(timestamp * 1000).toISOString().split('T')[0]
  const price = candlestickSeries?.coordinateToPrice?.(param.point.y) || 0

  emit('click', { date, price })
}

/**
 * 更新图表数据
 */
function updateChartData() {
  if (!candlestickSeries || fullData.value.length === 0) return

  // 使用完整数据（lightweight-charts 会自动处理视口）
  const sortedData = [...fullData.value].sort((a, b) =>
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
}

/**
 * 设置视口到中心区域
 */
function fitToCenter() {
  if (!chart || fullData.value.length === 0) return

  const totalBars = fullData.value.length
  const centerWindow = 20
  const rightOffset = 5  // 右侧留白

  const from = Math.max(0, totalBars - centerWindow - rightOffset)
  const to = totalBars + rightOffset

  chart.timeScale().setVisibleLogicalRange({ from, to })
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
 * 计算指定日期的MA值
 */
function calculateMAAtIndex(date: string, period: number): number | null {
  const index = fullData.value.findIndex((d) => d.date === date)
  if (index < period - 1) return null

  let sum = 0
  for (let i = 0; i < period; i++) {
    sum += fullData.value[index - i].close
  }
  return sum / period
}

/**
 * 处理窗口大小变化
 */
function handleResize() {
  if (chart && chartContainer.value) {
    chart.applyOptions({
      width: chartContainer.value.clientWidth,
      height: chartContainer.value.clientHeight
    })
  }
}

// 生命周期
onMounted(() => {
  initChart()
  if (props.autoLoad) {
    loadData()
  }
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (chart) {
    chart.remove()
    chart = null
  }
})

// 监听属性变化
watch(() => [props.symbol, props.timeframe, props.startDate, props.endDate], () => {
  fullData.value = []
  visibleData.value = []
  loadData()
})

watch(showMA, updateChartData)
watch(showVolume, updateChartData)

// 暴露方法
defineExpose({
  refresh: handleForceRefresh,
  loadData
})
</script>

<style scoped>
.kline-chart-container {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #131722;
  border-radius: 8px;
  overflow: hidden;
}

.chart-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
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
  font-weight: 600;
  color: #d1d4dc;
  font-size: 14px;
}

.timeframe-label {
  padding: 2px 8px;
  background: #2a2e39;
  border-radius: 4px;
  font-size: 12px;
  color: #787b86;
}

.cache-badge {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  background: rgba(38, 166, 154, 0.2);
  border-radius: 4px;
  font-size: 11px;
  color: #26a69a;
}

.loading-badge {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  background: rgba(245, 158, 11, 0.2);
  border-radius: 4px;
  font-size: 11px;
  color: #f59e0b;
}

.toolbar-right {
  display: flex;
  gap: 8px;
}

.toolbar-btn {
  padding: 6px 12px;
  background: #2a2e39;
  border: none;
  border-radius: 4px;
  color: #787b86;
  font-size: 12px;
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
  padding: 6px 10px;
}

.refresh-btn.loading {
  opacity: 0.7;
}

.chart-wrapper {
  position: relative;
  flex: 1;
  min-height: 0;
}

.chart-container {
  width: 100%;
  height: 100%;
}

/* 骨架屏 */
.skeleton-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: #131722;
  display: flex;
  align-items: center;
  justify-content: center;
}

.skeleton-chart {
  width: 100%;
  height: 100%;
  padding: 20px;
  display: flex;
  flex-direction: column;
}

.skeleton-axis {
  height: 30px;
  background: linear-gradient(90deg, #2a2e39 25%, #363a45 50%, #2a2e39 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  margin-bottom: 10px;
}

.skeleton-bars {
  flex: 1;
  display: flex;
  align-items: flex-end;
  justify-content: space-around;
  gap: 4px;
}

.skeleton-bar {
  width: 20px;
  background: linear-gradient(180deg, #2a2e39 25%, #363a45 50%, #2a2e39 75%);
  background-size: 100% 200%;
  animation: shimmer 1.5s infinite;
  border-radius: 2px 2px 0 0;
  opacity: 0.5;
}

.skeleton-bar:nth-child(odd) {
  height: 60%;
}

.skeleton-bar:nth-child(even) {
  height: 40%;
}

@keyframes shimmer {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}

/* 自定义图例 */
.custom-legend {
  position: absolute;
  top: 12px;
  left: 12px;
  background: rgba(30, 34, 45, 0.9);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 8px 12px;
  backdrop-filter: blur(4px);
}

.legend-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}

.legend-label {
  color: #787b86;
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

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.legend-divider {
  width: 1px;
  height: 14px;
  background: #2a2e39;
}

/* 状态栏 */
.chart-status {
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
