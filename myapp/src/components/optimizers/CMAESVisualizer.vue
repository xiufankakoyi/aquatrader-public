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

// 生成椭圆数据（根据进度动态变化）
const ellipseData = computed(() => {
  const progress = props.progress / 100;
  
  // 椭圆中心（目标点）
  const centerX = 50;
  const centerY = 50;
  
  // 初始椭圆很大，逐渐缩小
  const initialRadiusX = 40;
  const initialRadiusY = 25;
  const finalRadiusX = 5;
  const finalRadiusY = 3;
  
  const radiusX = initialRadiusX * (1 - progress * 0.9) + finalRadiusX;
  const radiusY = initialRadiusY * (1 - progress * 0.9) + finalRadiusY;
  
  // 旋转角度（模拟协方差矩阵的旋转）
  const rotation = progress * 45; // 旋转45度
  
  // 生成椭圆边界点
  const points: Array<[number, number]> = [];
  for (let i = 0; i <= 360; i += 5) {
    const angle = (i * Math.PI) / 180;
    const x = centerX + radiusX * Math.cos(angle) * Math.cos((rotation * Math.PI) / 180) 
              - radiusY * Math.sin(angle) * Math.sin((rotation * Math.PI) / 180);
    const y = centerY + radiusX * Math.cos(angle) * Math.sin((rotation * Math.PI) / 180) 
              + radiusY * Math.sin(angle) * Math.cos((rotation * Math.PI) / 180);
    points.push([x, y]);
  }
  
  return { center: [centerX, centerY], points, radiusX, radiusY };
});

// 生成采样点（椭圆内部随机点）
const samplePoints = computed(() => {
  const { center, radiusX, radiusY } = ellipseData.value;
  const points: Array<[number, number]> = [];
  const count = 15;
  
  for (let i = 0; i < count; i++) {
    // 在椭圆内生成随机点
    const angle = Math.random() * 2 * Math.PI;
    const r = Math.sqrt(Math.random()); // 均匀分布
    const x = center[0] + radiusX * r * Math.cos(angle);
    const y = center[1] + radiusY * r * Math.sin(angle);
    points.push([x, y]);
  }
  
  return points;
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
  
  const { center, points } = ellipseData.value;
  const samples = samplePoints.value;
  
  const option: EChartsOption = {
    backgroundColor: 'transparent',
    grid: {
      top: '5%',
      right: '5%',
      bottom: '15%',
      left: '5%',
      containLabel: false
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: '#334155',
      textStyle: {
        color: '#f1f5f9',
        fontSize: 12
      }
    },
    xAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { show: false }
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { show: false }
    },
    series: [
      // 椭圆边界
      {
        name: '搜索分布',
        type: 'line',
        data: points,
        smooth: true,
        lineStyle: {
          color: '#a855f7',
          width: 2,
          opacity: 0.8
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(168, 85, 247, 0.3)' },
              { offset: 1, color: 'rgba(168, 85, 247, 0)' }
            ]
          }
        },
        symbol: 'none',
        animationDuration: 600,
        animationEasing: 'cubicOut'
      },
      // 采样点
      {
        name: '采样点',
        type: 'scatter',
        data: samples,
        symbolSize: 6,
        itemStyle: {
          color: '#f472b6',
          borderColor: '#fff',
          borderWidth: 1,
          shadowBlur: 8,
          shadowColor: 'rgba(244, 114, 182, 0.5)'
        },
        animationDuration: 600,
        animationEasing: 'cubicOut'
      },
      // 中心点（最优解）
      {
        type: 'scatter',
        data: [center],
        symbolSize: 12,
        itemStyle: {
          color: '#10b981',
          borderColor: '#fff',
          borderWidth: 2,
          shadowBlur: 20,
          shadowColor: 'rgba(16, 185, 129, 0.9)'
        },
        z: 10
      }
    ],
    animationDuration: 600,
    animationEasing: 'cubicOut'
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
        CMA-ES · 第 {{ props.iteration || 1 }} 次迭代
      </span>
      <span class="text-slate-400">
        协方差椭圆收缩中… 均值 + 协方差矩阵
      </span>
    </div>
  </div>
</template>

<style scoped>
</style>
