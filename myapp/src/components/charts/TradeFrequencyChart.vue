<template>
  <div class="w-full h-full relative">
    <div ref="chartContainer" class="w-full h-full"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, markRaw } from 'vue';
import * as echarts from 'echarts';

interface Props {
  trades: any[];
  syncXAxis?: string;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const initChart = () => {
  if (!chartContainer.value) return;
  chartInstance = markRaw(echarts.init(chartContainer.value));
  updateChart();
};

const updateChart = () => {
  if (!chartInstance) return;

  // Aggregate trades by date
  const counts: Record<string, number> = {};
  props.trades.forEach(t => {
    const d = t.entryDate || t.date;
    if (d) counts[d] = (counts[d] || 0) + 1;
  });

  // Sort dates
  const sortedDates = Object.keys(counts).sort();
  const data = sortedDates.map(d => [d, counts[d]]);

  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1e222d',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc' },
      formatter: (params: any) => {
        const p = params[0];
        return `<div class="font-mono text-[11px]">
          <div class="text-[#787b86]">${p.axisValue}</div>
          <div class="text-[#2962ff]">交易次数: ${p.value[1]}</div>
        </div>`;
      }
    },
    grid: {
      top: 10,
      left: 10,
      right: 10,
      bottom: 20,
      containLabel: true
    },
    xAxis: {
      type: 'category',
      axisLine: { show: false },
      axisLabel: { 
        color: '#787b86',
        fontSize: 9,
        show: true
      },
      splitLine: { show: false }
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: { 
        color: '#787b86',
        fontSize: 9
      },
      splitLine: { show: false }
    },
    series: [{
      name: 'Trade Frequency',
      type: 'bar',
      data: data,
      itemStyle: {
        color: '#2962ff',
        opacity: 0.8
      },
      barWidth: '60%'
    }]
  };

  chartInstance.setOption(option);
};

watch(() => props.trades, updateChart, { deep: true });

onMounted(() => {
  initChart();
  window.addEventListener('resize', () => chartInstance?.resize());
});

onUnmounted(() => {
  chartInstance?.dispose();
});
</script>
