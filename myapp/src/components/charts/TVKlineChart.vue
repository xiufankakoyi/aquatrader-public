<template>
  <div class="tv-kline-container">
    <!-- 左上角 OHLC 信息流 -->
    <div v-if="showLegend" class="ohlc-header">
      <div class="symbol-row">
        <span class="symbol-name">{{ symbol || '沪深300' }}</span>
        <span class="timeframe">1D</span>
      </div>
      <div v-if="legendData" class="ohlc-row">
        <span class="ohlc-item">
          <span class="ohlc-label">开</span>
          <span class="ohlc-value" :class="getPriceChangeClass(legendData.open, legendData.close)">
            {{ formatPrice(legendData.open) }}
          </span>
        </span>
        <span class="ohlc-item">
          <span class="ohlc-label">高</span>
          <span class="ohlc-value" :class="getPriceChangeClass(legendData.high, legendData.close)">
            {{ formatPrice(legendData.high) }}
          </span>
        </span>
        <span class="ohlc-item">
          <span class="ohlc-label">低</span>
          <span class="ohlc-value" :class="getPriceChangeClass(legendData.low, legendData.close)">
            {{ formatPrice(legendData.low) }}
          </span>
        </span>
        <span class="ohlc-item">
          <span class="ohlc-label">收</span>
          <span class="ohlc-value main" :class="getPriceChangeClass(legendData.close, legendData.open)">
            {{ formatPrice(legendData.close) }}
          </span>
        </span>
        <span v-if="legendData.changePercent !== undefined" class="ohlc-item change-item">
          <span class="ohlc-value change" :class="legendData.changePercent >= 0 ? 'up' : 'down'">
            {{ legendData.changePercent >= 0 ? '+' : '' }}{{ legendData.changePercent.toFixed(2) }}%
          </span>
        </span>
      </div>
      <div v-if="legendData && (legendData.ma5 !== null || legendData.ma10 !== null)" class="ma-row">
        <span v-if="legendData.ma5 !== null" class="ma-item">
          <span class="ma-line" style="background: #fbbf24;"></span>
          <span class="ma-label">MA5</span>
          <span class="ma-value">{{ formatPrice(legendData.ma5) }}</span>
        </span>
        <span v-if="legendData.ma10 !== null" class="ma-item">
          <span class="ma-line" style="background: #60a5fa;"></span>
          <span class="ma-label">MA10</span>
          <span class="ma-value">{{ formatPrice(legendData.ma10) }}</span>
        </span>
        <span v-if="legendData.volume !== null" class="ma-item volume">
          <span class="ma-label">成交量</span>
          <span class="ma-value">{{ formatVolume(legendData.volume) }}</span>
        </span>
      </div>
    </div>
    
    <div ref="chartContainer" class="chart-container"></div>
    
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
import { ref, onMounted, onUnmounted, watch, watchEffect, nextTick } from 'vue';
import { createChart, type IChartApi, type ISeriesApi, type CandlestickData, type HistogramData, type UTCTimestamp, type LineData, CandlestickSeries, HistogramSeries, LineSeries, type SeriesMarker } from 'lightweight-charts';
import { useChartDrawing, type DrawingType } from '../../composables/useChartDrawing';

/**
 * TVLC K线图组件 - TradingView 风格
 * 使用 TradingView Lightweight Charts 实现高性能K线展示
 */

interface KlineData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  ma5?: number;
  ma10?: number;
}

interface TradeMarker {
  date: string;
  action: 'buy' | 'sell';
  price: number;
  quantity: number;
  symbol?: string;
  profitLoss?: number;
}

interface Props {
  data: KlineData[];
  markers?: TradeMarker[];
  showLegend?: boolean;
  showVolume?: boolean;
  showMA?: boolean;
  isLoading?: boolean;
  symbol?: string;
  activeTool?: DrawingType;
  magnetMode?: boolean;
  lockDrawings?: boolean;
  hideDrawings?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  data: () => [],
  markers: () => [],
  showLegend: true,
  showVolume: true,
  showMA: true,
  isLoading: false,
  symbol: '',
  activeTool: 'crosshair',
  magnetMode: false,
  lockDrawings: false,
  hideDrawings: false
});

const emit = defineEmits<{
  hover: [data: { date: string; open: number; high: number; low: number; close: number; volume?: number }];
  click: [data: { date: string; price: number }];
}>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chart: IChartApi | null = null;
let candlestickSeries: ISeriesApi<'Candlestick'> | null = null;
let volumeSeries: ISeriesApi<'Histogram'> | null = null;
let ma5Series: ISeriesApi<'Line'> | null = null;

// 绘图工具
const drawing = useChartDrawing();
let isMouseDown = false;
let ma10Series: ISeriesApi<'Line'> | null = null;

// 存储最新的数据引用（用于闭包中访问）
const currentDataRef = ref<KlineData[]>([]);

// 使用 watchEffect 确保数据同步
watchEffect(() => {
  const data = props.data;
  console.log('[TVKlineChart watchEffect] props.data length:', data?.length);
  if (data && data.length > 0) {
    currentDataRef.value = [...data];
    console.log('[TVKlineChart watchEffect] currentDataRef updated, length:', currentDataRef.value.length);
  }
});

// TradingView 风格配色 - 深色主题
const UP_COLOR = '#22c55e';      // 上涨绿色
const DOWN_COLOR = '#ef4444';    // 下跌红色
const UP_BORDER_COLOR = '#22c55e';
const DOWN_BORDER_COLOR = '#ef4444';

// 图例数据
const legendData = ref<{
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  ma5: number | null;
  ma10: number | null;
  volume: number | null;
  changePercent: number | null;
} | null>(null);

/**
 * 日期转时间戳
 */
function dateToTimestamp(date: string): number {
  return new Date(date).getTime() / 1000;
}

/**
 * 格式化价格
 */
function formatPrice(price: number | null): string {
  if (price === null || price === undefined) return '--';
  return price.toFixed(2);
}

/**
 * 格式化成交量
 */
function formatVolume(volume: number | null): string {
  if (volume === null || volume === undefined) return '--';
  if (volume >= 100000000) {
    return (volume / 100000000).toFixed(2) + '亿';
  }
  if (volume >= 10000) {
    return (volume / 10000).toFixed(2) + '万';
  }
  return volume.toString();
}

/**
 * 获取价格变化样式类
 */
function getPriceChangeClass(current: number | null, reference: number | null): string {
  if (current === null || reference === null) return 'neutral';
  if (current > reference) return 'up';
  if (current < reference) return 'down';
  return 'neutral';
}

/**
 * 初始化图表
 */
function initChart() {
  if (!chartContainer.value) return;

  // 创建图表 - TradingView 深色主题
  chart = createChart(chartContainer.value, {
    layout: {
      background: { color: '#0A0A0A' },  // TradingView 深色背景
      textColor: '#d1d4dc',
      fontSize: 11,
      fontFamily: 'JetBrains Mono, -apple-system, BlinkMacSystemFont, sans-serif'
    },
    grid: {
      vertLines: {
        color: '#2B2B43',  // 极淡的蓝灰
        style: 2,  // 虚线
        visible: true
      },
      horzLines: {
        color: '#2B2B43',
        style: 2,
        visible: true
      }
    },
    crosshair: {
      mode: 1,
      vertLine: {
        color: '#758696',
        width: 1,
        style: 2,
        labelBackgroundColor: '#758696'
      },
      horzLine: {
        color: '#758696',
        width: 1,
        style: 2,
        labelBackgroundColor: '#758696'
      }
    },
    rightPriceScale: {
      borderColor: '#2B2B43',
      scaleMargins: {
        top: 0.1,
        bottom: 0.2  // 给成交量留空间
      }
    },
    timeScale: {
      borderColor: '#2B2B43',
      timeVisible: false,
      secondsVisible: false,
      tickMarkFormatter: (time: number) => {
        const date = new Date(time * 1000);
        const month = date.getMonth() + 1;
        const day = date.getDate();
        return `${month}/${day}`;
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

  // 创建K线系列 - 去掉描边，更锐利
  candlestickSeries = chart.addSeries(CandlestickSeries, {
    upColor: UP_COLOR,
    downColor: DOWN_COLOR,
    borderUpColor: UP_COLOR,  // 描边颜色等于填充色
    borderDownColor: DOWN_COLOR,
    wickUpColor: UP_COLOR,
    wickDownColor: DOWN_COLOR,
    priceFormat: {
      type: 'price',
      precision: 2,
      minMove: 0.01
    }
  });

  // 设置K线数据
  updateCandlestickData();

  // 添加成交量系列（如果需要）- 放在底部小区域
  if (props.showVolume) {
    volumeSeries = chart.addSeries(HistogramSeries, {
      color: '#26a69a',
      priceFormat: {
        type: 'volume'
      },
      priceScaleId: 'volume'
    });
    
    // 单独配置成交量价格轴的scaleMargins
    chart.priceScale('volume').applyOptions({
      scaleMargins: {
        top: 0.9,  // 成交量从90%位置开始，只占据底部10%
        bottom: 0.02
      },
      visible: false  // 隐藏成交量价格轴刻度
    });
    
    updateVolumeData();
  }

  // 添加MA线（如果需要）- 使用更醒目的颜色
  if (props.showMA) {
    ma5Series = chart.addSeries(LineSeries, {
      color: '#fbbf24',  // 金黄色
      lineWidth: 1.5,
      lastValueVisible: false,
      priceLineVisible: false
    });

    ma10Series = chart.addSeries(LineSeries, {
      color: '#60a5fa',  // 亮蓝色
      lineWidth: 1.5,
      lastValueVisible: false,
      priceLineVisible: false
    });

    updateMAData();
  }

  // 更新标记
  updateMarkers();

  // 监听点击事件 - 用于绘图和发送 emit
  chart.subscribeClick((param) => {
    console.log('[subscribeClick] param:', { 
      time: param.time, 
      logical: param.logical, 
      point: param.point,
      paneIndex: param.paneIndex 
    });
    
    // 使用 ref 存储的最新数据
    const currentData = currentDataRef.value;
    console.log('[subscribeClick] currentData.length:', currentData.length);
    
    // 使用 logical 坐标获取时间
    let timestamp: number | null = null;
    let price: number | null = null;
    
    if (param.logical !== undefined && param.point) {
      // logical 是数据索引（从 0 开始）
      const dataIndex = Math.round(param.logical);
      const dataLength = currentData.length;
      
      console.log('[subscribeClick] logical:', param.logical, 'dataIndex:', dataIndex, 'dataLength:', dataLength);
      
      if (dataIndex >= 0 && dataIndex < dataLength) {
        const dateStr = currentData[dataIndex].date;
        timestamp = new Date(dateStr).getTime() / 1000;
        console.log('[subscribeClick] 从 logical 获取时间, dateStr:', dateStr, 'timestamp:', timestamp);
      } else if (dataLength > 0) {
        // 如果索引超出范围，使用最近的数据点
        const clampedIndex = Math.max(0, Math.min(dataIndex, dataLength - 1));
        const dateStr = currentData[clampedIndex].date;
        timestamp = new Date(dateStr).getTime() / 1000;
        console.log('[subscribeClick] 使用边界索引, clampedIndex:', clampedIndex, 'dateStr:', dateStr, 'timestamp:', timestamp);
      }
      
      // 获取价格
      price = candlestickSeries?.coordinateToPrice(param.point.y) || null;
      console.log('[subscribeClick] price:', price, 'point.y:', param.point.y);
    }
    
    if (timestamp === null || price === null) {
      console.log('[subscribeClick] 无法获取时间或价格, timestamp:', timestamp, 'price:', price);
      return;
    }
    
    const date = new Date(timestamp * 1000).toISOString().split('T')[0];
    
    console.log('[subscribeClick] date:', date, 'price:', price, 'activeTool:', props.activeTool);
    
    emit('click', { date, price });
    
    // 处理绘图点击
    if (props.activeTool !== 'crosshair') {
      console.log('[subscribeClick] 处理绘图点击, isDrawing:', drawing.isDrawing.value);
      
      if (!drawing.isDrawing.value) {
        // 第一次点击：开始绘制
        drawing.startDrawing(timestamp, price, currentData);
      } else {
        // 第二次点击：完成绘制
        drawing.finishDrawing(timestamp, price, currentData);
      }
    }
  });

  // 监听十字光标移动事件用于绘图更新
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !candlestickSeries) {
      return;
    }

    const timestamp = param.time as number;
    const date = new Date(timestamp * 1000).toISOString().split('T')[0];
    
    // 获取当前光标位置的K线数据
    const candleData = candlestickSeries.data().find(d => d.time === timestamp);
    
    if (candleData) {
      // 找到原始数据中的对应项
      const originalData = props.data.find(item => item.date === date);
      
      // 计算涨跌幅
      const changePercent = ((candleData.close - candleData.open) / candleData.open) * 100;
      
      legendData.value = {
        open: candleData.open,
        high: candleData.high,
        low: candleData.low,
        close: candleData.close,
        ma5: originalData?.ma5 ?? null,
        ma10: originalData?.ma10 ?? null,
        volume: originalData?.volume ?? null,
        changePercent: changePercent
      };

      emit('hover', {
        date,
        open: candleData.open,
        high: candleData.high,
        low: candleData.low,
        close: candleData.close,
        volume: originalData?.volume
      });
      
      // 更新绘图预览
      if (isMouseDown && param.point) {
        const price = candlestickSeries.coordinateToPrice(param.point.y);
        if (price !== null) {
          drawing.updateDrawing(timestamp, price, props.data);
        }
      }
    }
  });

  // 监听鼠标事件用于绘图
  setupDrawingEvents();

  // 初始化绘图工具
  if (chart && candlestickSeries) {
    drawing.init(chart, candlestickSeries);
  }

  // 适应容器大小
  handleResize();
}

/**
 * 设置绘图事件监听
 */
function setupDrawingEvents() {
  // 绘图事件现在通过 chart.subscribeClick 处理
  console.log('[TVKlineChart] 绘图事件监听已设置（使用 subscribeClick）');
}

// 测试绘制功能 - 已移除，绘图由 useChartDrawing 处理

/**
 * 更新K线数据
 */
function updateCandlestickData() {
  if (!candlestickSeries || props.data.length === 0) return;

  const candleData: CandlestickData[] = [];
  const len = props.data.length;
  
  for (let i = 0; i < len; i++) {
    const item = props.data[i];
    candleData.push({
      time: dateToTimestamp(item.date),
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close
    });
  }

  candlestickSeries.setData(candleData);
}

/**
 * 更新成交量数据
 */
function updateVolumeData() {
  if (!volumeSeries || props.data.length === 0) return;

  const volumeData: HistogramData[] = [];
  const len = props.data.length;
  
  for (let i = 0; i < len; i++) {
    const item = props.data[i];
    volumeData.push({
      time: dateToTimestamp(item.date),
      value: item.volume || 0,
      color: item.close >= item.open ? UP_COLOR : DOWN_COLOR
    });
  }

  volumeSeries.setData(volumeData);
}

/**
 * 更新MA数据
 */
function updateMAData() {
  if (!ma5Series || !ma10Series) {
    console.log('[TVKlineChart] updateMAData: MA系列未初始化');
    return;
  }
  if (props.data.length === 0) {
    console.log('[TVKlineChart] updateMAData: 数据为空');
    return;
  }

  const len = props.data.length;
  const ma5Data: LineData[] = [];
  const ma10Data: LineData[] = [];
  
  // 使用滑动窗口计算MA，避免重复求和
  let sum5 = 0;
  let sum10 = 0;
  
  for (let i = 0; i < len; i++) {
    const item = props.data[i];
    const time = dateToTimestamp(item.date);
    
    // MA5计算
    sum5 += item.close;
    if (i >= 5) {
      sum5 -= props.data[i - 5].close;
    }
    if (i >= 4) {
      ma5Data.push({ time, value: sum5 / 5 });
    }
    
    // MA10计算
    sum10 += item.close;
    if (i >= 10) {
      sum10 -= props.data[i - 10].close;
    }
    if (i >= 9) {
      ma10Data.push({ time, value: sum10 / 10 });
    }
  }

  console.log('[TVKlineChart] updateMAData: 数据长度=', len, 'MA5=', ma5Data.length, 'MA10=', ma10Data.length);
  ma5Series.setData(ma5Data);
  ma10Series.setData(ma10Data);
}

/**
 * 更新买卖标记
 */
function updateMarkers() {
  if (!candlestickSeries) return;
  if (props.markers.length === 0) {
    // 清空标记
    if (typeof candlestickSeries.setMarkers === 'function') {
      candlestickSeries.setMarkers([]);
    }
    return;
  }

  const markers: SeriesMarker<UTCTimestamp>[] = props.markers.map(marker => {
    const isBuy = marker.action === 'buy';
    return {
      time: dateToTimestamp(marker.date) as UTCTimestamp,
      position: isBuy ? 'belowBar' : 'aboveBar',
      color: isBuy ? '#ef4444' : '#22c55e',
      shape: isBuy ? 'arrowUp' : 'arrowDown',
      text: isBuy ? 'B' : 'S',
      size: 1.5
    };
  });

  if (typeof candlestickSeries.setMarkers === 'function') {
    candlestickSeries.setMarkers(markers);
  } else {
    console.warn('[TVKlineChart] setMarkers not available');
  }
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
 * 设置可见范围
 */
function setVisibleRange(from: string, to: string) {
  if (!chart) return;
  chart.timeScale().setVisibleRange({
    from: dateToTimestamp(from) as UTCTimestamp,
    to: dateToTimestamp(to) as UTCTimestamp
  });
}

/**
 * 跳转到指定日期
 */
function scrollToDate(date: string) {
  if (!chart) return;
  chart.timeScale().scrollToPosition(dateToTimestamp(date), true);
}

let dataUpdateTimeout: number | null = null;
let lastVisibleRange: { from: number; to: number } | null = null;

// 监听数据变化
watch(() => props.data, (newData, oldData) => {
  console.log('[TVKlineChart] props.data changed, length:', newData?.length);
  
  // 在更新数据前，保存当前可见范围
  if (chart && newData && oldData && newData.length > oldData.length) {
    const range = chart.timeScale().getVisibleRange();
    if (range) {
      lastVisibleRange = { from: range.from as number, to: range.to as number };
    }
  }
  
  // 防抖处理，避免频繁更新
  if (dataUpdateTimeout) {
    clearTimeout(dataUpdateTimeout);
  }
  
  dataUpdateTimeout = window.setTimeout(() => {
    nextTick(() => {
      handleResize();
      updateCandlestickData();
      updateVolumeData();
      if (props.showMA) {
        updateMAData();
      }
      updateMarkers();
      nextTick(() => {
        handleResize();
        // 如果是追加数据（长度增加），恢复之前的可见范围
        if (newData && oldData && newData.length > oldData.length && lastVisibleRange) {
          // 恢复之前的视图位置
          chart?.timeScale().setVisibleRange({
            from: lastVisibleRange.from as any,
            to: lastVisibleRange.to as any
          });
        } else if (!oldData || oldData.length === 0) {
          // 首次加载数据时，适应内容
          chart?.timeScale().fitContent();
        }
      });
    });
  }, 50);
}, { deep: false });

// 监听标记变化
watch(() => props.markers, () => {
  nextTick(() => {
    updateMarkers();
    // 同时更新MA，确保数据一致性
    if (props.showMA) {
      updateMAData();
    }
  });
}, { deep: true });

// 监听加载状态变化 - 当加载完成时重新调整图表大小
watch(() => props.isLoading, (loading) => {
  if (!loading) {
    // 加载完成后，给 DOM 一点时间来渲染
    setTimeout(() => {
      handleResize();
      chart?.timeScale().fitContent();
    }, 100);
  }
});

// 监听绘图工具变化
watch(() => props.activeTool, (newTool) => {
  console.log('[TVKlineChart] activeTool changed:', newTool);
  drawing.setTool(newTool);
});

watch(() => props.magnetMode, (enabled) => {
  if (enabled !== drawing.magnetMode.value) {
    drawing.toggleMagnetMode();
  }
});

watch(() => props.lockDrawings, (enabled) => {
  if (enabled !== drawing.lockDrawings.value) {
    drawing.toggleLockDrawings();
  }
});

watch(() => props.hideDrawings, (enabled) => {
  if (enabled !== drawing.hideDrawings.value) {
    drawing.toggleHideDrawings();
  }
});

// 监听 showMA 变化
watch(() => props.showMA, (newVal) => {
  if (newVal && ma5Series && ma10Series) {
    updateMAData();
  }
});

// 生命周期
onMounted(() => {
  nextTick(() => {
    initChart();
    window.addEventListener('resize', handleResize);
    // 延迟再次调整大小，确保容器已正确渲染（特别是弹窗场景）
    setTimeout(() => {
      handleResize();
      if (props.data.length > 0) {
        chart?.timeScale().fitContent();
      }
    }, 200);
  });
});

onUnmounted(() => {
  if (dataUpdateTimeout) {
    clearTimeout(dataUpdateTimeout);
  }
  window.removeEventListener('resize', handleResize);
  if (chart) {
    chart.remove();
    chart = null;
  }
});

// 暴露方法
defineExpose({
  setVisibleRange,
  scrollToDate,
  handleResize,
  removeSelectedDrawing: drawing.removeSelectedDrawing,
  clearAllDrawings: drawing.clearAllDrawings,
  redrawAll: drawing.redrawAll
});
</script>

<style scoped>
.tv-kline-container {
  position: relative;
  width: 100%;
  height: 100%;
  background: #0A0A0A;  /* TradingView 深色背景 */
}

.chart-container {
  width: 100%;
  height: 100%;
}

/* 左上角 OHLC 信息流 - TradingView 风格 */
.ohlc-header {
  position: absolute;
  top: 8px;
  left: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  pointer-events: none;
  z-index: 10;
  background: rgba(19, 23, 34, 0.8);
  padding: 6px 10px;
  border-radius: 4px;
  backdrop-filter: blur(4px);
}

.symbol-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.symbol-name {
  font-size: 14px;
  font-weight: 700;
  color: #d1d4dc;
}

.timeframe {
  font-size: 10px;
  font-weight: 600;
  color: #0A0A0A;
  background: #2962ff;
  padding: 2px 6px;
  border-radius: 3px;
}

.ohlc-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ohlc-item {
  display: flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
}

.ohlc-label {
  color: #787b86;
  font-weight: 500;
}

.ohlc-value {
  font-weight: 600;
  color: #d1d4dc;
}

.ohlc-value.main {
  font-size: 13px;
  font-weight: 700;
}

.ohlc-value.up {
  color: #22c55e;
}

.ohlc-value.down {
  color: #ef4444;
}

.ohlc-value.neutral {
  color: #d1d4dc;
}

.change-item {
  margin-left: 4px;
  padding-left: 8px;
  border-left: 1px solid #2B2B43;
}

.change {
  font-weight: 700;
}

.ma-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 2px;
  padding-top: 4px;
  border-top: 1px solid #2B2B43;
}

.ma-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-family: 'JetBrains Mono', monospace;
}

.ma-line {
  width: 10px;
  height: 2px;
  border-radius: 1px;
}

.ma-label {
  color: #787b86;
}

.ma-value {
  color: #d1d4dc;
  font-weight: 600;
}

.ma-item.volume {
  margin-left: 8px;
  padding-left: 8px;
  border-left: 1px solid #2B2B43;
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
