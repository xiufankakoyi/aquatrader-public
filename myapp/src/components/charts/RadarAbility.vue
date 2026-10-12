<template>
  <div class="radar-ability-container">
    <div class="flex items-center justify-between mb-4">
      <h3 class="text-lg font-semibold text-white">策略能力雷达</h3>
      <div v-if="feasibilityScore !== null" class="text-sm">
        <span class="text-slate-400">可行性评分: </span>
        <span class="font-bold" :class="getScoreClass(feasibilityScore)">{{ feasibilityScore }} 分</span>
      </div>
    </div>
    <div ref="chartContainer" class="w-full h-80"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

interface RadarScores {
  excessReturn?: number;
  riskConsistency?: number;
  maxDrawdown?: number;
  tradingQuality?: number;
  antiOverfitting?: number;
}

interface Props {
  scores: RadarScores | null;
  benchmark?: RadarScores | null;
  feasibilityScore?: number | null;
}

const props = withDefaults(defineProps<Props>(), {
  benchmark: null,
  feasibilityScore: null
});

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const radarLabels = ['收益', '稳定性', '风险控制', '流动性', '容量'];

function initChart() {
  if (!chartContainer.value) return;

  if (chartContainer.value.clientWidth === 0) {
    setTimeout(initChart, 100);
    return;
  }

  if (chartInstance) {
    chartInstance.dispose();
  }

  chartInstance = markRaw(echarts.init(chartContainer.value));
  updateChart();
}

function updateChart() {
  if (!chartInstance || !props.scores) return;

  const strategyData = [
    props.scores.excessReturn ?? 0,
    props.scores.riskConsistency ?? 0,
    props.scores.maxDrawdown ?? 0,
    props.scores.tradingQuality ?? 0,
    props.scores.antiOverfitting ?? 0
  ];

  const series: any[] = [{
    name: '当前策略',
    type: 'radar',
    data: [{
      value: strategyData,
      name: '当前策略',
      areaStyle: {
        color: 'rgba(16, 185, 129, 0.2)'
      },
      lineStyle: {
        color: '#10b981',
        width: 2
      },
      itemStyle: {
        color: '#10b981'
      }
    }]
  }];

  if (props.benchmark) {
    const benchmarkData = [
      props.benchmark.excessReturn ?? 0,
      props.benchmark.riskConsistency ?? 0,
      props.benchmark.maxDrawdown ?? 0,
      props.benchmark.tradingQuality ?? 0,
      props.benchmark.antiOverfitting ?? 0
    ];
    series.push({
      name: '上证500',
      type: 'radar',
      data: [{
        value: benchmarkData,
        name: '上证500',
        lineStyle: {
          color: '#94a3b8',
          width: 2,
          type: 'dashed'
        },
        itemStyle: {
          color: '#94a3b8'
        }
      }]
    });
  }

  const option: EChartsOption = {
    radar: {
      indicator: radarLabels.map(label => ({ name: label, max: 100 })),
      center: ['50%', '55%'],
      radius: '70%',
      axisName: {
        fontSize: 12,
        color: '#94a3b8'
      },
      splitArea: {
        show: true,
        areaStyle: {
          color: ['rgba(255, 255, 255, 0.05)', 'rgba(255, 255, 255, 0.02)']
        }
      },
      splitLine: {
        lineStyle: {
          color: '#334155'
        }
      }
    },
    series: series,
    tooltip: {
      trigger: 'item'
    },
    legend: {
      data: ['当前策略', ...(props.benchmark ? ['上证500'] : [])],
      bottom: 10,
      textStyle: {
        color: '#94a3b8'
      }
    }
  };

  chartInstance.setOption(option);
}

function getScoreClass(score: number): string {
  if (score >= 80) return 'text-red-400';
  if (score >= 50) return 'text-yellow-400';
  return 'text-green-400';
}

watch(() => [props.scores, props.benchmark], () => {
  updateChart();
}, { deep: true });

function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
  nextTick(() => {
    initChart();
    window.addEventListener('resize', handleResize);
  });
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});
</script>

<style scoped>
.radar-ability-container {
  width: 100%;
}
</style>

