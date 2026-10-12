<template>
  <div class="bg-[#1c202b] border border-[#2a2e39] rounded-xl p-4 h-full flex flex-col shadow-lg overflow-hidden relative">
    <div class="flex items-center justify-between mb-2">
      <h4 class="text-xs font-bold text-[#d1d4dc] uppercase tracking-wider flex items-center gap-2">
        <i class="fas fa-radar-ast text-indigo-400"></i>
        策略风险雷达
      </h4>
      <div class="flex items-center gap-1">
        <span class="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-pulse"></span>
        <span class="text-[10px] text-[#787b86] font-mono uppercase">Live</span>
      </div>
    </div>

    <div ref="radarRef" class="flex-1 w-full min-h-[180px]"></div>

    <!-- Stats summary -->
    <div class="grid grid-cols-2 gap-2 mt-2 pt-2 border-t border-[#2a2e39]">
      <div v-for="(val, key) in scores" :key="key" class="flex flex-col">
        <span class="text-[9px] text-[#787b86] uppercase truncate">{{ labelMap[key] }}</span>
        <span class="text-xs font-mono font-bold" :class="getColorClass(val)">{{ val }}%</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, markRaw, computed } from 'vue';
import * as echarts from 'echarts';
import { useBacktestStore } from '../store/backtestStore';

const backtestStore = useBacktestStore();
const radarRef = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const scores = computed(() => backtestStore.liveRiskScores);

const labelMap: Record<string, string> = {
  alpha: 'Alpha 强度',
  resistance: '回撤抵抗',
  plRatio: '盈亏比',
  concentration: '集中度',
  efficiency: '执行效率'
};

function getColorClass(val: number) {
  if (val > 80) return 'text-emerald-400';
  if (val > 50) return 'text-indigo-400';
  if (val > 30) return 'text-orange-400';
  return 'text-red-400';
}

function initChart() {
  if (!radarRef.value) return;
  chartInstance = markRaw(echarts.init(radarRef.value));
  updateChart();
}

function updateChart() {
  if (!chartInstance) return;

  const data = [
    scores.value.alpha,
    scores.value.resistance,
    scores.value.plRatio,
    scores.value.concentration,
    scores.value.efficiency
  ];

  const option: echarts.EChartsOption = {
    radar: {
      shape: 'polygon',
      indicator: [
        { name: 'Alpha', max: 100 },
        { name: 'Resistance', max: 100 },
        { name: 'P/L Ratio', max: 100 },
        { name: 'Conc.', max: 100 },
        { name: 'Eff.', max: 100 }
      ],
      radius: '65%',
      center: ['50%', '50%'],
      axisName: {
        color: '#787b86',
        fontSize: 10,
        fontWeight: 'bold'
      },
      splitLine: {
        lineStyle: {
          color: 'rgba(42, 46, 57, 0.5)'
        }
      },
      splitArea: {
        show: false
      },
      axisLine: {
        lineStyle: {
          color: 'rgba(42, 46, 57, 0.5)'
        }
      }
    },
    series: [
      {
        name: 'Risk Radar',
        type: 'radar',
        data: [
          {
            value: data,
            name: 'Scores',
            symbol: 'none',
            lineStyle: {
              width: 2,
              color: '#6366f1'
            },
            areaStyle: {
              color: new echarts.graphic.RadialGradient(0.5, 0.5, 1, [
                { offset: 0, color: 'rgba(99, 102, 241, 0.1)' },
                { offset: 1, color: 'rgba(99, 102, 241, 0.4)' }
              ])
            }
          }
        ]
      }
    ]
  };

  chartInstance.setOption(option);
}

watch(() => scores.value, () => {
  updateChart();
}, { deep: true });

onMounted(() => {
  initChart();
  window.addEventListener('resize', () => chartInstance?.resize());
});
</script>
