<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

const props = defineProps<{
  progress: number;
  iteration?: number;
  history?: Array<{ iteration: number; metric: number; params?: Record<string, any> }>;
}>();

const chartContainer = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 生成网格数据（打印机效果：从左到右，从上到下）
const gridData = computed(() => {
  const gridSize = 10; // 10x10网格
  const totalCells = gridSize * gridSize;
  const progress = props.progress / 100;
  const activeCount = Math.floor(totalCells * progress);
  
  const data: Array<[number, number, number]> = [];
  
  for (let i = 0; i < totalCells; i++) {
    const row = Math.floor(i / gridSize);
    const col = i % gridSize;
    
    // 是否已激活（打印机效果）
    const isActive = i < activeCount;
    
    // 生成随机分数（已激活的格子有分数，未激活的为0）
    const score = isActive ? 10 + Math.random() * 20 : 0;
    
    data.push([col, row, score]);
  }
  
  return data;
});

function initChart() {
  if (!chartContainer.value) return;
  
  chartInstance = echarts.init(chartContainer.value);
  updateChart();
  
  window.addEventListener('resize', handleResize);
}

function handleResize() {
  chartInstance?.resize();
}

function updateChart() {
  if (!chartInstance) return;
  
  const data = gridData.value;
  const gridSize = 10;
  
  const option: EChartsOption = {
    backgroundColor: 'transparent',
    grid: {
      top: '10%',
      right: '5%',
      bottom: '15%',
      left: '10%',
      containLabel: false
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: '#334155',
      textStyle: {
        color: '#f1f5f9',
        fontSize: 12
      },
      formatter: (params: any) => {
        const [x, y, score] = params.data;
        if (score === 0) {
          return `网格点 (${x}, ${y})<br/>未评估`;
        }
        return `网格点 (${x}, ${y})<br/>分数: ${score.toFixed(2)}`;
      }
    },
    xAxis: {
      type: 'value',
      min: -0.5,
      max: gridSize - 0.5,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: {
        show: true,
        lineStyle: {
          color: 'rgba(148, 163, 184, 0.1)'
        }
      }
    },
    yAxis: {
      type: 'value',
      min: -0.5,
      max: gridSize - 0.5,
      inverse: true, // 从上到下
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: {
        show: true,
        lineStyle: {
          color: 'rgba(148, 163, 184, 0.1)'
        }
      }
    },
    visualMap: {
      min: 0,
      max: 30,
      calculable: false,
      show: false,
      inRange: {
        color: ['#1e293b', '#334155', '#475569', '#6366f1', '#8b5cf6', '#a855f7']
      }
    },
    series: [
      {
        name: '网格搜索',
        type: 'scatter',
        data: data,
        symbolSize: (data: any) => {
          // 已激活的格子显示，未激活的不显示
          return data[2] > 0 ? 25 : 0;
        },
        itemStyle: {
          color: (params: any) => {
            const score = params.data[2];
            if (score === 0) return 'transparent';
            // 分数越高，颜色越亮
            const ratio = score / 30;
            if (ratio > 0.8) return '#a855f7';
            if (ratio > 0.6) return '#8b5cf6';
            if (ratio > 0.4) return '#6366f1';
            return '#475569';
          },
          borderColor: '#fff',
          borderWidth: 1,
          shadowBlur: 8,
          shadowColor: 'rgba(139, 92, 246, 0.5)'
        },
        animationDuration: 300,
        animationEasing: 'linear',
        animationDelay: (idx: number) => {
          // 打印机效果：按顺序延迟
          return idx * 20;
        }
      }
    ],
    animationDuration: 300,
    animationEasing: 'linear'
  };
  
  chartInstance.setOption(option, { notMerge: true, lazyUpdate: false });
}

watch(() => [props.progress, props.history], () => {
  if (chartInstance) {
    updateChart();
  }
}, { deep: true });

onMounted(() => {
  nextTick(() => {
    initChart();
  });
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});
</script>

<template>
  <div class="relative h-full w-full overflow-hidden bg-slate-950/40">
    <div ref="chartContainer" class="w-full h-full"></div>
    
    <div class="absolute inset-x-0 bottom-2 px-3 text-[11px] text-slate-300 flex justify-between pointer-events-none">
      <span>
        网格搜索 · 第 {{ props.iteration || 1 }} 次评估
      </span>
      <span class="text-slate-400">
        已评估 {{ Math.floor((props.progress / 100) * 100) }} / 100 个网格点
    </span>
    </div>
  </div>
</template>

<style scoped>
</style>
