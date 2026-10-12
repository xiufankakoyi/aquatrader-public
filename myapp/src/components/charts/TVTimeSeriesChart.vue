<template>
  <div class="tv-time-series-container">
    <div ref="chartContainer" class="chart-container"></div>
    
    <!-- 自定义图例 -->
    <div v-if="showLegend && legendData" class="custom-legend">
      <div class="legend-row">
        <span class="legend-item">
          <span class="legend-label">价格</span>
          <span class="legend-value" :class="getPriceChangeClass()">
            {{ formatPrice(legendData.price) }}
          </span>
        </span>
        <span class="legend-item">
          <span class="legend-label">涨跌</span>
          <span class="legend-value" :class="getPriceChangeClass()">
            {{ formatChange(legendData.change) }} ({{ formatChangePercent(legendData.changePercent) }})
          </span>
        </span>
        <span v-if="legendData.volume" class="legend-item">
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue';
import { createChart, type IChartApi, type ISeriesApi, type LineData, type UTCTimestamp, type AreaData } from 'lightweight-charts';

/**
 * TVLC 分时图组件
 * 使用 TradingView Lightweight Charts 实现高性能分时图展示
 * 适用于实时行情回放和盯盘场景
 */

interface TimeSeriesData {
  time: number; // Unix timestamp in seconds
  price: number;
  volume?: number;
}

interface Props {
  data: TimeSeriesData[];
  basePrice?: number; // 基准价格（用于计算涨跌）
  showLegend?: boolean;
  showArea?: boolean; // 是否显示面积图
  lineColor?: string;
  upColor?: string;
  downColor?: string;
  isLoading?: boolean;
  playbackCursor?: number | null; // 回放光标位置（timestamp）
}

const props = withDefaults(defineProps<Props>(), {
  data: () => [],
  basePrice: 0,
  showLegend: true,
  showArea: true,
  lineColor: '#2962ff',
  upColor: '#ef4444',
  downColor: '#22c55e',
  isLoading: false,
  playbackCursor: null
});

const emit = defineEmits<{
  hover: [data: { time: number; price: number; volume?: number }];
  click: [data: { time: number; price: number }];
}>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chart: IChartApi | null = null;
let lineSeries: ISeriesApi<'Line'> | null = null;
let areaSeries: ISeriesApi<'Area'> | null = null;
let cursorSeries: ISeriesApi<'Line'> | null = null;

const legendData = ref<{
  price: number;
  change: number;
  changePercent: number;
  volume?: number;
} | null>(null);

/**
 * 计算基准价格（如果没有提供）
 */
const computedBasePrice = computed(() => {
  if (props.basePrice > 0) return props.basePrice;
  if (props.data.length > 0) return props.data[0].price;
  return 0;
});

/**
 * 格式化价格
 */
function formatPrice(price: number): string {
  if (price === undefined || price === null) return '--';
  return price.toFixed(2);
}

/**
 * 格式化涨跌额
 */
function formatChange(change: number): string {
  const sign = change >= 0 ? '+' : '';
  return `${sign}${change.toFixed(2)}`;
}

/**
 * 格式化涨跌幅百分比
 */
function formatChangePercent(percent: number): string {
  const sign = percent >= 0 ? '+' : '';
  return `${sign}${percent.toFixed(2)}%`;
}

/**
 * 格式化成交量
 */
function formatVolume(volume: number): string {
  if (volume >= 100000000) {
    return (volume / 100000000).toFixed(2) + '亿';
  } else if (volume >= 10000) {
    return (volume / 10000).toFixed(2) + '万';
  }
  return volume.toFixed(0);
}

/**
 * 获取价格变化颜色类
 */
function getPriceChangeClass(): string {
  if (!legendData.value) return 'neutral';
  const change = legendData.value.change;
  if (change > 0) return 'up';
  if (change < 0) return 'down';
  return 'neutral';
}

/**
 * 初始化图表
 */
function initChart() {
  if (!chartContainer.value) return;

  // 销毁旧图表
  if (chart) {
    chart.remove();
    chart = null;
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
      mode: 1,
      vertLine: {
        color: '#787b86',
        width: 1,
        style: 2,
        labelBackgroundColor: '#2a2e39'
      },
      horzLine: {
        color: '#787b86',
        width: 1,
        style: 2,
        labelBackgroundColor: '#2a2e39'
      }
    },
    rightPriceScale: {
      borderColor: '#2a2e39',
      scaleMargins: {
        top: 0.1,
        bottom: 0.1
      }
    },
    timeScale: {
      borderColor: '#2a2e39',
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time: number) => {
        const date = new Date(time * 1000);
        const hours = date.getHours().toString().padStart(2, '0');
        const minutes = date.getMinutes().toString().padStart(2, '0');
        return `${hours}:${minutes}`;
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
  });

  // 创建面积图系列（如果需要）
  if (props.showArea) {
    areaSeries = chart.addAreaSeries({
      lineColor: props.lineColor,
      topColor: props.lineColor + '40', // 25% 透明度
      bottomColor: props.lineColor + '05', // 2% 透明度
      lineWidth: 2,
      lastValueVisible: true,
      priceFormat: {
        type: 'price',
        precision: 2,
        minMove: 0.01
      }
    });
  } else {
    // 创建线图系列
    lineSeries = chart.addLineSeries({
      color: props.lineColor,
      lineWidth: 2,
      lastValueVisible: true,
      priceFormat: {
        type: 'price',
        precision: 2,
        minMove: 0.01
      }
    });
  }

  // 创建回放光标系列
  cursorSeries = chart.addLineSeries({
    color: '#fbbf24',
    lineWidth: 1,
    lineStyle: 2, // 虚线
    lastValueVisible: false,
    priceLineVisible: false
  });

  // 设置数据
  updateData();
  updateCursor();

  // 监听十字光标移动
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point) {
      return;
    }

    const series = areaSeries || lineSeries;
    if (series) {
      const data = param.seriesData.get(series);
      if (data) {
        const lineData = data as LineData;
        const timestamp = param.time as number;
        const price = lineData.value;
        
        // 查找对应的成交量
        const originalData = props.data.find(d => d.time === timestamp);
        const volume = originalData?.volume;
        
        // 计算涨跌
        const basePrice = computedBasePrice.value;
        const change = price - basePrice;
        const changePercent = basePrice > 0 ? (change / basePrice) * 100 : 0;
        
        legendData.value = {
          price,
          change,
          changePercent,
          volume
        };

        emit('hover', {
          time: timestamp,
          price,
          volume
        });
      }
    }
  });

  // 监听点击事件
  chart.subscribeClick((param) => {
    if (param.time && param.point) {
      const timestamp = param.time as number;
      const series = areaSeries || lineSeries;
      const price = series?.coordinateToPrice(param.point.y) || 0;
      
      emit('click', { time: timestamp, price });
    }
  });

  // 适应容器大小
  handleResize();
}

/**
 * 更新数据
 */
function updateData() {
  if (props.data.length === 0) return;

  const lineData: LineData[] = props.data.map(item => ({
    time: item.time as UTCTimestamp,
    value: item.price
  }));

  if (areaSeries) {
    areaSeries.setData(lineData);
  } else if (lineSeries) {
    lineSeries.setData(lineData);
  }

  // 初始化图例数据为最新值
  const lastData = props.data[props.data.length - 1];
  if (lastData) {
    const basePrice = computedBasePrice.value;
    const change = lastData.price - basePrice;
    const changePercent = basePrice > 0 ? (change / basePrice) * 100 : 0;
    
    legendData.value = {
      price: lastData.price,
      change,
      changePercent,
      volume: lastData.volume
    };
  }
}

/**
 * 更新回放光标
 */
function updateCursor() {
  if (!cursorSeries || !props.playbackCursor) return;

  // 找到光标位置对应的价格
  const cursorData = props.data.find(d => d.time >= props.playbackCursor!);
  if (!cursorData) return;

  // 创建垂直光标线
  const cursorLineData: LineData[] = [
    { time: props.playbackCursor as UTCTimestamp, value: cursorData.price * 0.9 },
    { time: props.playbackCursor as UTCTimestamp, value: cursorData.price * 1.1 }
  ];

  cursorSeries.setData(cursorLineData);
}

/**
 * 处理窗口大小变化
 */
function handleResize() {
  if (chart && chartContainer.value) {
    const { width, height } = chartContainer.value.getBoundingClientRect();
    chart.applyOptions({ width, height });
  }
}

/**
 * 跳转到指定时间
 */
function scrollToTime(time: number) {
  if (!chart) return;
  chart.timeScale().scrollToPosition(time as UTCTimestamp, true);
}

/**
 * 设置可见范围
 */
function setVisibleRange(from: number, to: number) {
  if (!chart) return;
  chart.timeScale().setVisibleRange({
    from: from as UTCTimestamp,
    to: to as UTCTimestamp
  });
}

// 监听数据变化
watch(() => props.data, () => {
  nextTick(() => {
    updateData();
  });
}, { deep: true });

// 监听回放光标变化
watch(() => props.playbackCursor, () => {
  nextTick(() => {
    updateCursor();
  });
});

// 生命周期
onMounted(() => {
  nextTick(() => {
    initChart();
    window.addEventListener('resize', handleResize);
  });
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  if (chart) {
    chart.remove();
    chart = null;
  }
});

// 暴露方法
defineExpose({
  scrollToTime,
  setVisibleRange,
  handleResize
});
</script>

<style scoped>
.tv-time-series-container {
  position: relative;
  width: 100%;
  height: 100%;
  background: transparent;
}

.chart-container {
  width: 100%;
  height: 100%;
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
</style>
