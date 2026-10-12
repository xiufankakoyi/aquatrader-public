<template>
  <div class="pattern-chart">
    <div ref="chartRef" class="chart"></div>
    <div v-if="rows.length === 0" class="empty">暂无 K 线事件数据</div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import type { PatternEventRow } from '@/api/pattern';

const props = defineProps<{
  rows: PatternEventRow[];
  matchDate?: string;
}>();

const chartRef = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

onMounted(() => {
  if (chartRef.value) {
    chart = echarts.init(chartRef.value);
    renderChart();
    window.addEventListener('resize', resizeChart);
  }
});

onUnmounted(() => {
  window.removeEventListener('resize', resizeChart);
  chart?.dispose();
  chart = null;
});

watch(
  () => [props.rows, props.matchDate] as const,
  () => renderChart(),
  { deep: true }
);

function resizeChart(): void {
  chart?.resize();
}

function renderChart(): void {
  if (!chart) return;
  const rows = props.rows.filter((row) => row.open !== null && row.high !== null && row.low !== null && row.close !== null);
  const dates = rows.map((row) => row.trade_date);
  const kline = rows.map((row) => [Number(row.open), Number(row.close), Number(row.low), Number(row.high)]);
  const volumes = rows.map((row, index) => [index, Number(row.volume || 0), Number(row.close || 0) >= Number(row.open || 0) ? 1 : -1]);
  const matchIndex = props.matchDate ? dates.indexOf(props.matchDate) : -1;
  const option: EChartsOption = {
    backgroundColor: '#020617',
    animation: false,
    grid: [
      { left: 48, right: 18, top: 24, height: '62%' },
      { left: 48, right: 18, top: '76%', height: '16%' },
    ],
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#111827',
      borderColor: '#334155',
      textStyle: { color: '#e2e8f0' },
    },
    xAxis: [
      { type: 'category', data: dates, axisLine: { lineStyle: { color: '#334155' } }, axisLabel: { color: '#94a3b8' } },
      { type: 'category', data: dates, gridIndex: 1, axisLine: { lineStyle: { color: '#334155' } }, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, axisLine: { lineStyle: { color: '#334155' } }, splitLine: { lineStyle: { color: '#1f2937' } }, axisLabel: { color: '#94a3b8' } },
      { scale: true, gridIndex: 1, axisLine: { lineStyle: { color: '#334155' } }, splitLine: { show: false }, axisLabel: { color: '#64748b' } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], start: 0, end: 100, bottom: 4, height: 18, borderColor: '#1f2937' },
    ],
    series: [
      {
        type: 'candlestick',
        name: 'K线',
        data: kline,
        itemStyle: {
          color: '#22c55e',
          color0: '#ef4444',
          borderColor: '#22c55e',
          borderColor0: '#ef4444',
        },
        markLine: matchIndex >= 0 ? {
          symbol: 'none',
          lineStyle: { color: '#fbbf24', type: 'dashed', width: 1 },
          label: { color: '#fbbf24', formatter: '命中日' },
          data: [{ xAxis: matchIndex }],
        } : undefined,
      },
      {
        type: 'bar',
        name: '成交量',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
        itemStyle: {
          color: (params: { data: unknown }) => {
            const data = params.data as number[];
            return data[2] >= 0 ? 'rgba(34,197,94,0.45)' : 'rgba(239,68,68,0.45)';
          },
        },
      },
    ],
  };
  chart.setOption(option, true);
}
</script>

<style scoped>
.pattern-chart {
  position: relative;
  min-height: 360px;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #020617;
}

.chart {
  height: 360px;
}

.empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #64748b;
  pointer-events: none;
}
</style>
