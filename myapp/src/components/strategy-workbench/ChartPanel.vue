<template>
  <div class="chart-panel">
    <!-- 图表工具栏 -->
    <div class="chart-toolbar">
      <div class="toolbar-left">
        <span class="panel-title">回测图表</span>
      </div>
      
      <div class="toolbar-center">
        <!-- 时间轴进度条 (替代播放器) -->
        <div v-if="isLoading || equitySeries.length > 0" class="timeline-control">
          <button
            class="control-btn"
            @click="togglePlay"
            :title="isPlaying ? '暂停' : '播放'"
          >
            <i :class="isPlaying ? 'fas fa-pause' : 'fas fa-play'"></i>
          </button>
          
          <div class="timeline-slider">
            <div class="timeline-track">
              <div
                class="timeline-progress"
                :style="{ width: timelineProgress + '%' }"
              ></div>
            </div>
            <input
              v-model.number="currentTimelineIndex"
              type="range"
              :min="0"
              :max="equitySeries.length - 1"
              class="timeline-input"
              @input="handleTimelineChange"
            />
          </div>
          
          <span class="timeline-date">{{ currentDate }}</span>
        </div>
      </div>
      
      <div class="toolbar-right">
        <button
          class="control-btn"
          :class="{ active: chartScale === 'log' }"
          @click="chartScale = chartScale === 'linear' ? 'log' : 'linear'"
          title="切换线性/对数坐标"
        >
          {{ chartScale === 'linear' ? '线性' : '对数' }}
        </button>
        
        <button
          class="control-btn"
          @click="resetZoom"
          title="重置缩放"
        >
          <i class="fas fa-compress"></i>
        </button>
        
        <button
          class="control-btn"
          :class="{ active: showTrades }"
          @click="showTrades = !showTrades"
          title="显示/隐藏买卖点"
        >
          <i class="fas fa-map-marker-alt"></i>
        </button>
      </div>
    </div>

    <!-- 主图表区域 -->
    <div class="charts-container">
      <!-- 主图：净值曲线 -->
      <div class="chart-wrapper main-chart">
        <div class="chart-header">
          <span class="chart-label">净值曲线</span>
          <div v-if="hoverData" class="hover-info">
            <span class="hover-date">{{ hoverData.date }}</span>
            <span class="hover-equity" :class="getEquityClass(hoverData.equity)">
              {{ formatEquity(hoverData.equity) }}
            </span>
          </div>
        </div>
        <div ref="mainChartRef" class="chart-content"></div>
      </div>

      <!-- 副图 1：仓位占比 -->
      <div class="chart-wrapper sub-chart">
        <div class="chart-header">
          <span class="chart-label">仓位占比</span>
        </div>
        <div ref="positionChartRef" class="chart-content"></div>
      </div>

      <!-- 副图 2：回撤幅度 -->
      <div class="chart-wrapper sub-chart">
        <div class="chart-header">
          <span class="chart-label">回撤幅度</span>
        </div>
        <div ref="drawdownChartRef" class="chart-content"></div>
      </div>

      <!-- 副图 3：交易频率 -->
      <div class="chart-wrapper sub-chart">
        <div class="chart-header">
          <span class="chart-label">交易频率</span>
        </div>
        <div ref="frequencyChartRef" class="chart-content"></div>
      </div>
    </div>

    <!-- 加载状态 -->
    <div v-if="isLoading && equitySeries.length === 0" class="loading-overlay">
      <div class="loading-content">
        <div class="spinner"></div>
        <span class="loading-text">正在计算回测...</span>
      </div>
    </div>

    <!-- 空状态 -->
    <div v-else-if="equitySeries.length === 0" class="empty-state">
      <i class="fas fa-chart-line empty-icon"></i>
      <p class="empty-text">点击「运行回测」查看收益曲线</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue';
import * as echarts from 'echarts';
import type { ECharts, EChartsOption } from 'echarts';
import type { Trade } from '../../types/backtest';

// ============================================
// Props & Emits
// ============================================
interface Props {
  equitySeries: Array<{ date: string; equity: number }>;
  benchmarkSeries: Array<{ date: string; equity: number }>;
  positionSeries: Array<{ date: string; position: number }>;
  drawdownSeries: Array<{ date: string; drawdown: number }>;
  tradeFrequencyData: Array<{ date: string; count: number }>;
  trades: Trade[];
  isLoading: boolean;
  syncDate: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  hover: [data: { date: string; equity?: number }];
  'date-select': [date: string];
}>();

// ============================================
// 状态
// ============================================
const mainChartRef = ref<HTMLElement>();
const positionChartRef = ref<HTMLElement>();
const drawdownChartRef = ref<HTMLElement>();
const frequencyChartRef = ref<HTMLElement>();

let mainChart: ECharts | null = null;
let positionChart: ECharts | null = null;
let drawdownChart: ECharts | null = null;
let frequencyChart: ECharts | null = null;

const chartScale = ref<'linear' | 'log'>('linear');
const showTrades = ref(true);
const isPlaying = ref(false);
const currentTimelineIndex = ref(0);
const hoverData = ref<{ date: string; equity?: number } | null>(null);

let playInterval: ReturnType<typeof setInterval> | null = null;

// ============================================
// 计算属性
// ============================================
const timelineProgress = computed(() => {
  if (props.equitySeries.length === 0) return 0;
  return (currentTimelineIndex.value / (props.equitySeries.length - 1)) * 100;
});

const currentDate = computed(() => {
  if (props.equitySeries.length === 0) return '--';
  const index = Math.min(currentTimelineIndex.value, props.equitySeries.length - 1);
  return props.equitySeries[index]?.date || '--';
});

// ============================================
// 图表配置
// ============================================
const commonGridConfig = {
  left: '3%',
  right: '4%',
  top: '10%',
  bottom: '5%',
  containLabel: true,
};

const commonAxisConfig = {
  axisLine: { lineStyle: { color: '#2a2e39' } },
  axisLabel: { color: '#787b86', fontSize: 10 },
  splitLine: { show: false },
};

// 主图配置
function getMainChartOption(): EChartsOption {
  const dates = props.equitySeries.map(d => d.date);
  const equityData = props.equitySeries.map(d => d.equity);
  const benchmarkData = props.benchmarkSeries.map(d => d.equity);

  // 买卖点标记
  const tradeMarks = showTrades.value
    ? props.trades.map(trade => ({
        name: trade.action.toUpperCase(),
        coord: [trade.date, trade.price],
        value: trade.action.toUpperCase(),
        itemStyle: {
          color: trade.action === 'buy' ? '#f23645' : '#089981',
        },
      }))
    : [];

  return {
    backgroundColor: 'transparent',
    grid: commonGridConfig,
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross',
        crossStyle: { color: '#787b86', width: 1, type: 'dashed' },
      },
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 11 },
      formatter: (params: any) => {
        const date = params[0]?.axisValue;
        let html = `<div style="font-weight:600;margin-bottom:4px">${date}</div>`;
        params.forEach((p: any) => {
          const color = p.color;
          const value = p.value?.toFixed(2) || '--';
          html += `<div style="display:flex;align-items:center;gap:6px">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color}"></span>
            <span>${p.seriesName}: ${value}</span>
          </div>`;
        });
        return html;
      },
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
      label: { backgroundColor: '#2a2e39' },
    },
    xAxis: {
      type: 'category',
      data: dates,
      ...commonAxisConfig,
      axisPointer: { label: { show: true } },
    },
    yAxis: {
      type: chartScale.value === 'log' ? 'log' : 'value',
      scale: true,
      ...commonAxisConfig,
      splitLine: { show: true, lineStyle: { color: '#1e222d', type: 'dashed' } },
      axisLabel: {
        color: '#787b86',
        fontSize: 10,
        formatter: (value: number) => value.toFixed(0),
      },
    },
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1, 2, 3],
        start: 0,
        end: 100,
      },
      {
        type: 'slider',
        xAxisIndex: [0, 1, 2, 3],
        start: 0,
        end: 100,
        height: 16,
        bottom: 0,
        borderColor: 'transparent',
        backgroundColor: '#1e222d',
        fillerColor: 'rgba(41, 98, 255, 0.2)',
        handleStyle: { color: '#2962ff' },
        textStyle: { color: '#787b86', fontSize: 10 },
      },
    ],
    series: [
      {
        name: '策略净值',
        type: 'line',
        data: equityData,
        smooth: false,
        symbol: 'none',
        lineStyle: { width: 2, color: '#2962ff' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(41, 98, 255, 0.2)' },
            { offset: 1, color: 'rgba(41, 98, 255, 0)' },
          ]),
        },
        markPoint: {
          data: tradeMarks,
          symbol: 'pin',
          symbolSize: 30,
          label: { fontSize: 8, color: '#fff' },
        },
      },
      {
        name: '基准净值',
        type: 'line',
        data: benchmarkData,
        smooth: false,
        symbol: 'none',
        lineStyle: { width: 1.5, color: '#787b86', type: 'dashed' },
      },
    ],
  };
}

// 仓位图配置
function getPositionChartOption(): EChartsOption {
  const dates = props.positionSeries.map(d => d.date);
  const positionData = props.positionSeries.map(d => d.position * 100);

  return {
    backgroundColor: 'transparent',
    grid: { ...commonGridConfig, top: '15%', bottom: '15%' },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 11 },
      formatter: (params: any) => {
        const date = params[0]?.axisValue;
        const value = params[0]?.value?.toFixed(1) || '--';
        return `<div style="font-weight:600">${date}</div>
                <div>仓位: ${value}%</div>`;
      },
    },
    xAxis: {
      type: 'category',
      data: dates,
      ...commonAxisConfig,
      show: false,
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      ...commonAxisConfig,
      splitLine: { show: true, lineStyle: { color: '#1e222d', type: 'dashed' } },
      axisLabel: {
        color: '#787b86',
        fontSize: 10,
        formatter: '{value}%',
      },
    },
    series: [
      {
        name: '仓位',
        type: 'line',
        data: positionData,
        smooth: false,
        symbol: 'none',
        lineStyle: { width: 1.5, color: '#f5a623' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(245, 166, 35, 0.3)' },
            { offset: 1, color: 'rgba(245, 166, 35, 0)' },
          ]),
        },
      },
    ],
  };
}

// 回撤图配置
function getDrawdownChartOption(): EChartsOption {
  const dates = props.drawdownSeries.map(d => d.date);
  const drawdownData = props.drawdownSeries.map(d => d.drawdown * 100);

  return {
    backgroundColor: 'transparent',
    grid: { ...commonGridConfig, top: '15%', bottom: '15%' },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 11 },
      formatter: (params: any) => {
        const date = params[0]?.axisValue;
        const value = params[0]?.value?.toFixed(2) || '--';
        return `<div style="font-weight:600">${date}</div>
                <div>回撤: ${value}%</div>`;
      },
    },
    xAxis: {
      type: 'category',
      data: dates,
      ...commonAxisConfig,
      show: false,
    },
    yAxis: {
      type: 'value',
      max: 0,
      ...commonAxisConfig,
      splitLine: { show: true, lineStyle: { color: '#1e222d', type: 'dashed' } },
      axisLabel: {
        color: '#787b86',
        fontSize: 10,
        formatter: '{value}%',
      },
    },
    series: [
      {
        name: '回撤',
        type: 'line',
        data: drawdownData,
        smooth: false,
        symbol: 'none',
        lineStyle: { width: 1.5, color: '#f23645' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(242, 54, 69, 0.3)' },
            { offset: 1, color: 'rgba(242, 54, 69, 0)' },
          ]),
        },
      },
    ],
  };
}

// 交易频率图配置
function getFrequencyChartOption(): EChartsOption {
  const dates = props.tradeFrequencyData.map(d => d.date);
  const frequencyData = props.tradeFrequencyData.map(d => d.count);

  return {
    backgroundColor: 'transparent',
    grid: { ...commonGridConfig, top: '15%', bottom: '20%' },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 11 },
      formatter: (params: any) => {
        const date = params[0]?.axisValue;
        const value = params[0]?.value || '--';
        return `<div style="font-weight:600">${date}</div>
                <div>交易次数: ${value}</div>`;
      },
    },
    xAxis: {
      type: 'category',
      data: dates,
      ...commonAxisConfig,
      axisLabel: { show: true, interval: 'auto' },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      ...commonAxisConfig,
      splitLine: { show: true, lineStyle: { color: '#1e222d', type: 'dashed' } },
      axisLabel: { color: '#787b86', fontSize: 10 },
    },
    series: [
      {
        name: '交易次数',
        type: 'bar',
        data: frequencyData,
        barWidth: '60%',
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#f23645' },
            { offset: 1, color: 'rgba(242, 54, 69, 0.3)' },
          ]),
        },
      },
    ],
  };
}

// ============================================
// 方法
// ============================================
function initCharts() {
  // 检查 DOM 元素是否有有效尺寸
  const hasValidSize = (el: HTMLElement | undefined) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  if (mainChartRef.value && hasValidSize(mainChartRef.value)) {
    mainChart = echarts.init(mainChartRef.value);
    mainChart.on('updateAxisPointer', (params: any) => {
      const date = params.axesInfo[0]?.value;
      const dataIndex = params.dataIndex;
      if (date && props.equitySeries[dataIndex]) {
        hoverData.value = {
          date,
          equity: props.equitySeries[dataIndex].equity,
        };
        emit('hover', hoverData.value);
      }
    });
    mainChart.on('click', (params: any) => {
      if (params.axisValue) {
        emit('date-select', params.axisValue);
      }
    });
  }

  if (positionChartRef.value && hasValidSize(positionChartRef.value)) {
    positionChart = echarts.init(positionChartRef.value);
  }

  if (drawdownChartRef.value && hasValidSize(drawdownChartRef.value)) {
    drawdownChart = echarts.init(drawdownChartRef.value);
  }

  if (frequencyChartRef.value && hasValidSize(frequencyChartRef.value)) {
    frequencyChart = echarts.init(frequencyChartRef.value);
  }

  // 联动图表
  if (mainChart && positionChart && drawdownChart && frequencyChart) {
    echarts.connect([mainChart, positionChart, drawdownChart, frequencyChart]);
  }
}

function updateCharts() {
  if (mainChart) {
    mainChart.setOption(getMainChartOption(), true);
  }
  if (positionChart) {
    positionChart.setOption(getPositionChartOption(), true);
  }
  if (drawdownChart) {
    drawdownChart.setOption(getDrawdownChartOption(), true);
  }
  if (frequencyChart) {
    frequencyChart.setOption(getFrequencyChartOption(), true);
  }
}

function resetZoom() {
  mainChart?.dispatchAction({
    type: 'dataZoom',
    start: 0,
    end: 100,
  });
}

function togglePlay() {
  if (isPlaying.value) {
    stopPlay();
  } else {
    startPlay();
  }
}

function startPlay() {
  if (props.equitySeries.length === 0) return;
  
  isPlaying.value = true;
  playInterval = setInterval(() => {
    if (currentTimelineIndex.value < props.equitySeries.length - 1) {
      currentTimelineIndex.value++;
      handleTimelineChange();
    } else {
      stopPlay();
    }
  }, 100);
}

function stopPlay() {
  isPlaying.value = false;
  if (playInterval) {
    clearInterval(playInterval);
    playInterval = null;
  }
}

function handleTimelineChange() {
  const date = props.equitySeries[currentTimelineIndex.value]?.date;
  if (date) {
    emit('date-select', date);
    
    // 高亮图表中的对应位置
    mainChart?.dispatchAction({
      type: 'showTip',
      seriesIndex: 0,
      dataIndex: currentTimelineIndex.value,
    });
  }
}

function formatEquity(value: number): string {
  if (value === undefined || value === null) return '--';
  return value.toFixed(2);
}

function getEquityClass(value: number): string {
  if (!props.equitySeries.length) return '';
  const initial = props.equitySeries[0]?.equity || 1;
  return value >= initial ? 'positive' : 'negative';
}

// ============================================
// 监听
// ============================================
watch(() => props.equitySeries, updateCharts, { deep: true });
watch(() => props.benchmarkSeries, updateCharts, { deep: true });
watch(() => props.positionSeries, updateCharts, { deep: true });
watch(() => props.drawdownSeries, updateCharts, { deep: true });
watch(() => props.tradeFrequencyData, updateCharts, { deep: true });
watch(() => props.trades, updateCharts, { deep: true });
watch(chartScale, updateCharts);
watch(showTrades, updateCharts);

watch(() => props.syncDate, (date) => {
  if (!date || !mainChart) return;
  const index = props.equitySeries.findIndex(d => d.date === date);
  if (index !== -1) {
    currentTimelineIndex.value = index;
    mainChart.dispatchAction({
      type: 'showTip',
      seriesIndex: 0,
      dataIndex: index,
    });
  }
});

// ============================================
// 生命周期
// ============================================
onMounted(() => {
  // 使用 nextTick 确保 DOM 已渲染并且有尺寸
  nextTick(() => {
    initCharts();
    updateCharts();
  });

  window.addEventListener('resize', () => {
    mainChart?.resize();
    positionChart?.resize();
    drawdownChart?.resize();
    frequencyChart?.resize();
  });
});

onBeforeUnmount(() => {
  stopPlay();
  mainChart?.dispose();
  positionChart?.dispose();
  drawdownChart?.dispose();
  frequencyChart?.dispose();
});
</script>

<style scoped>
.chart-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  position: relative;
}

/* 图表工具栏 */
.chart-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 36px;
  padding: 0 12px;
  background-color: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.toolbar-left,
.toolbar-center,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-center {
  flex: 1;
  justify-content: center;
  max-width: 400px;
}

.panel-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-primary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

/* 时间轴控制 */
.timeline-control {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.control-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  color: var(--text-secondary);
  font-size: 9px;
  cursor: pointer;
  transition: all 0.2s;
  flex-shrink: 0;
}

.control-btn:hover {
  background-color: var(--bg-hover);
  border-color: var(--border-hover);
  color: var(--text-primary);
}

.control-btn.active {
  background-color: var(--accent-primary);
  border-color: var(--accent-primary);
  color: white;
}

.timeline-slider {
  flex: 1;
  position: relative;
  height: 16px;
}

.timeline-track {
  position: absolute;
  top: 50%;
  left: 0;
  right: 0;
  height: 3px;
  background-color: var(--bg-tertiary);
  border-radius: 2px;
  transform: translateY(-50%);
}

.timeline-progress {
  height: 100%;
  background-color: var(--accent-primary);
  border-radius: 2px;
  transition: width 0.1s;
}

.timeline-input {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  opacity: 0;
  cursor: pointer;
}

.timeline-date {
  font-size: 10px;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace;
  min-width: 70px;
  text-align: right;
}

/* 图表容器 */
.charts-container {
  flex: 1;
  display: grid;
  grid-template-rows: 2fr 1fr 1fr 1fr;
  gap: 1px;
  background-color: var(--border-color);
  overflow: hidden;
}

.chart-wrapper {
  background-color: var(--bg-primary);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 24px;
  padding: 0 12px;
  flex-shrink: 0;
}

.chart-label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.hover-info {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
}

.hover-date {
  color: var(--text-muted);
}

.hover-equity {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600;
}

.hover-equity.positive {
  color: var(--color-up);
}

.hover-equity.negative {
  color: var(--color-down);
}

.chart-content {
  flex: 1;
  min-height: 0;
}

/* 加载状态 */
.loading-overlay {
  position: absolute;
  inset: 36px 0 0 0;
  background-color: rgba(19, 23, 34, 0.9);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
}

.loading-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 2px solid var(--border-color);
  border-top-color: var(--accent-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.loading-text {
  font-size: 12px;
  color: var(--text-secondary);
}

/* 空状态 */
.empty-state {
  position: absolute;
  inset: 36px 0 0 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--text-muted);
}

.empty-icon {
  font-size: 48px;
  opacity: 0.3;
}

.empty-text {
  font-size: 12px;
}

/* 响应式 */
@media (max-width: 767px) {
  .chart-toolbar {
    flex-wrap: wrap;
    height: auto;
    padding: 8px;
    gap: 8px;
  }
  
  .toolbar-center {
    order: -1;
    width: 100%;
    max-width: none;
  }
  
  .charts-container {
    grid-template-rows: repeat(4, 1fr);
  }
}
</style>
