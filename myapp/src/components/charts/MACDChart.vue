<template>
  <div class="macd-chart-container" ref="chartContainer"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue';
import { createChart, HistogramSeries, LineSeries } from 'lightweight-charts';
import type { IChartApi, ISeriesApi } from 'lightweight-charts';

interface KlineData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

const props = defineProps<{
  data: KlineData[];
  height?: number;
}>();

const chartContainer = ref<HTMLElement | null>(null);
let chart: IChartApi | null = null;
let histogramSeries: ISeriesApi<'Histogram'> | null = null;
let difSeries: ISeriesApi<'Line'> | null = null;
let deaSeries: ISeriesApi<'Line'> | null = null;

function dateToTimestamp(dateStr: string): number {
  const date = new Date(dateStr);
  return Math.floor(date.getTime() / 1000);
}

/**
 * 手动计算EMA
 */
function calculateEMA(values: number[], period: number): number[] {
  const result: number[] = [];
  const multiplier = 2 / (period + 1);
  
  let ema = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      result.push(NaN);
    } else if (i === period - 1) {
      result.push(ema);
    } else {
      ema = (values[i] - ema) * multiplier + ema;
      result.push(ema);
    }
  }
  
  return result;
}

/**
 * 计算MACD指标
 * MACD = EMA(12) - EMA(26)
 * Signal = EMA(MACD, 9)
 * Histogram = MACD - Signal
 */
function calculateMACD(data: KlineData[]) {
  console.log('[MACDChart] calculateMACD called, data length:', data?.length);
  
  if (!data || data.length < 35) {
    console.log('[MACDChart] 数据不足，跳过计算');
    return { histogram: [], dif: [], dea: [] };
  }

  const closes = data.map(d => d.close);
  
  const ema12 = calculateEMA(closes, 12);
  const ema26 = calculateEMA(closes, 26);
  
  const macdLine: number[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (isNaN(ema12[i]) || isNaN(ema26[i])) {
      macdLine.push(NaN);
    } else {
      macdLine.push(ema12[i] - ema26[i]);
    }
  }
  
  const signalLine = calculateEMA(macdLine.filter(v => !isNaN(v)), 9);
  
  const histogram: { time: any; value: number; color: string }[] = [];
  const dif: { time: any; value: number }[] = [];
  const dea: { time: any; value: number }[] = [];
  
  let signalIndex = 0;
  const startCalcIndex = 25;
  
  for (let i = startCalcIndex; i < closes.length; i++) {
    if (isNaN(macdLine[i])) continue;
    
    const time = dateToTimestamp(data[i].date) as any;
    const macdValue = macdLine[i];
    
    if (signalIndex < signalLine.length) {
      const signalValue = signalLine[signalIndex];
      const histogramValue = macdValue - signalValue;
      
      histogram.push({
        time,
        value: histogramValue,
        color: histogramValue >= 0 ? '#22c55e' : '#ef4444'
      });
      
      dif.push({ time, value: macdValue });
      dea.push({ time, value: signalValue });
      
      signalIndex++;
    }
  }

  console.log('[MACDChart] 计算结果: histogram=', histogram.length, 'dif=', dif.length, 'dea=', dea.length);

  return { histogram, dif, dea };
}

function initChart() {
  if (!chartContainer.value) return;

  try {
    const rect = chartContainer.value.getBoundingClientRect();
    const width = rect.width || 300;
    const height = props.height || 120;

    chart = createChart(chartContainer.value, {
      width,
      height,
      layout: {
        background: { color: 'transparent' },
        textColor: '#9ca3af',
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.05)' },
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.1)',
        scaleMargins: {
          top: 0.1,
          bottom: 0.1,
        },
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.1)',
        visible: false,
      },
      crosshair: {
        mode: 1,
      },
    });

    // v5.x API: 使用 addSeries + Series 类型
    histogramSeries = chart.addSeries(HistogramSeries, {
      priceFormat: {
        type: 'price',
        precision: 4,
      },
    });

    difSeries = chart.addSeries(LineSeries, {
      color: '#3b82f6',
      lineWidth: 1,
      priceFormat: {
        type: 'price',
        precision: 4,
      },
    });

    deaSeries = chart.addSeries(LineSeries, {
      color: '#f59e0b',
      lineWidth: 1,
      priceFormat: {
        type: 'price',
        precision: 4,
      },
    });

    updateData();
  } catch (error) {
    console.error('[MACDChart] 初始化失败:', error);
  }
}

function updateData() {
  if (!chart || !histogramSeries || !difSeries || !deaSeries) return;
  if (!props.data || props.data.length === 0) return;

  try {
    const { histogram, dif, dea } = calculateMACD(props.data);

    // 过滤掉 NaN 和无效值
    const validHistogram = histogram.filter(d => !isNaN(d.value) && isFinite(d.value));
    const validDif = dif.filter(d => !isNaN(d.value) && isFinite(d.value));
    const validDea = dea.filter(d => !isNaN(d.value) && isFinite(d.value));

    if (validHistogram.length > 0) {
      histogramSeries.setData(validHistogram);
    }
    if (validDif.length > 0) {
      difSeries.setData(validDif);
    }
    if (validDea.length > 0) {
      deaSeries.setData(validDea);
    }
  } catch (error) {
    console.error('[MACDChart] 更新数据失败:', error);
  }
}

function handleResize() {
  if (chart && chartContainer.value) {
    try {
      const { width } = chartContainer.value.getBoundingClientRect();
      chart.applyOptions({ width });
    } catch (error) {
      console.error('[MACDChart] 调整大小失败:', error);
    }
  }
}

let updateTimeout: number | null = null;

watch(() => props.data, () => {
  if (updateTimeout) {
    clearTimeout(updateTimeout);
  }
  updateTimeout = window.setTimeout(() => {
    nextTick(() => {
      updateData();
    });
  }, 100);
}, { deep: true });

onMounted(() => {
  nextTick(() => {
    initChart();
  });
  window.addEventListener('resize', handleResize);
});

onUnmounted(() => {
  if (updateTimeout) {
    clearTimeout(updateTimeout);
  }
  window.removeEventListener('resize', handleResize);
  if (chart) {
    try {
      chart.remove();
    } catch (e) {
      console.error('[MACDChart] 清理失败:', e);
    }
    chart = null;
  }
});

defineExpose({
  chart,
  setVisibleRange: (from: number, to: number) => {
    if (chart) {
      chart.timeScale().setVisibleRange({ from: from as any, to: to as any });
    }
  },
  scrollToRealTime: () => {
    if (chart) {
      chart.timeScale().scrollToRealTime();
    }
  }
});
</script>

<style scoped>
.macd-chart-container {
  width: 100%;
  min-height: 120px;
}
</style>
