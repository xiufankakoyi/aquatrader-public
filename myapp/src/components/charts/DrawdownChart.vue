<template>
  <div class="w-full h-full relative">
    <div ref="chartContainer" class="w-full h-full"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, markRaw } from 'vue';
import * as echarts from 'echarts';

interface Props {
  equitySeries: Array<{ date: string; equity: number }>;
  syncXAxis?: string; // For crosshair sync
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

  // Calculate drawdowns
  let peak = -Infinity;
  const drawdownData = props.equitySeries.map(item => {
    if (item.equity > peak) peak = item.equity;
    const dd = ((item.equity / peak) - 1) * 100;
    return [item.date, dd];
  });

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
          <div class="text-[#f23645]">回撤: ${p.value[1].toFixed(2)}%</div>
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
      boundaryGap: false,
      axisLine: { show: false },
      axisLabel: { show: false }, 
      splitLine: { show: false } // Let CSS dot-grid through
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: { 
        color: '#787b86',
        fontSize: 9,
        formatter: (v: number) => `${v}%`
      },
      splitLine: { show: false }, // Let CSS dot-grid through
      max: 0
    },
    series: [{
      name: 'Drawdown',
      type: 'line',
      data: drawdownData,
      symbol: 'none',
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(242, 54, 69, 0.3)' },
          { offset: 1, color: 'rgba(242, 54, 69, 0)' }
        ])
      },
      lineStyle: {
        color: '#f23645',
        width: 1.5
      }
    }],
    axisPointer: {
      link: { xAxisIndex: 'all' }
    }
  };

  chartInstance.setOption(option);
};

// Syncing axis if needed
watch(() => props.syncXAxis, (newDate) => {
  if (chartInstance && newDate) {
    chartInstance.dispatchAction({
      type: 'showTip',
      dataIndex: props.equitySeries.findIndex(d => d.date === newDate)
    });
  }
});

watch(() => props.equitySeries, updateChart, { deep: true });

onMounted(() => {
  initChart();
  window.addEventListener('resize', () => chartInstance?.resize());
});

onUnmounted(() => {
  chartInstance?.dispose();
});
</script>
