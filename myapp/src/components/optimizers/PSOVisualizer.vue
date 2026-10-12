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

// 生成等高线数据（模拟参数空间地形）
function generateContourData() {
  const data: number[][] = [];
  const xRange = 100;
  const yRange = 100;
  
  for (let x = 0; x <= xRange; x += 5) {
    for (let y = 0; y <= yRange; y += 5) {
      // 创建一个有多个峰值的函数（模拟多峰优化问题）
      const centerX = 70;
      const centerY = 70;
      const dist = Math.sqrt((x - centerX) ** 2 + (y - centerY) ** 2);
      const value = 30 - dist * 0.3 + Math.sin(x * 0.1) * 2 + Math.cos(y * 0.1) * 2;
      data.push([x, y, Math.max(0, value)]);
    }
  }
  return data;
}

// 生成粒子位置（根据进度和历史数据）
const particleData = computed(() => {
  const particles: Array<[number, number, number]> = [];
  const particleCount = 30;
  const progress = props.progress / 100;
  
  // 最优位置（目标点）
  const targetX = 70;
  const targetY = 70;
  
  for (let i = 0; i < particleCount; i++) {
    // 初始位置：随机分布
    const initialX = 20 + (i % 10) * 8;
    const initialY = 20 + Math.floor(i / 10) * 8;
    
    // 根据进度，粒子逐渐向目标点聚拢
    const convergence = Math.min(1, progress * 1.2);
    const noise = (1 - convergence) * 15; // 噪声随收敛度减小
    
    const x = initialX + (targetX - initialX) * convergence + (Math.random() - 0.5) * noise;
    const y = initialY + (targetY - initialY) * convergence + (Math.random() - 0.5) * noise;
    
    // 计算分数（距离目标越近分数越高）
    const dist = Math.sqrt((x - targetX) ** 2 + (y - targetY) ** 2);
    const score = Math.max(0, 30 - dist * 0.3);
    
    particles.push([x, y, score]);
  }
  
  return particles;
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
  
  const contourData = generateContourData();
  const particles = particleData.value;
  
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
      },
      formatter: (params: any) => {
        if (params.seriesName === '等高线') {
          return `参数空间<br/>X: ${params.data[0]}<br/>Y: ${params.data[1]}<br/>分数: ${params.data[2].toFixed(2)}`;
        } else {
          return `粒子 ${params.dataIndex + 1}<br/>X: ${params.data[0].toFixed(1)}<br/>Y: ${params.data[1].toFixed(1)}<br/>分数: ${params.data[2].toFixed(2)}`;
        }
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
    visualMap: {
      min: 0,
      max: 30,
      calculable: false,
      show: false,
      inRange: {
        color: ['#1e293b', '#334155', '#475569', '#64748b', '#94a3b8', '#cbd5e1']
      }
    },
    series: [
      // 等高线背景
      {
        name: '等高线',
        type: 'scatter',
        data: contourData,
        symbolSize: 0,
        itemStyle: {
          opacity: 0.3
        },
        silent: true
      },
      // 粒子群
      {
        name: '粒子群',
        type: 'scatter',
        data: particles,
        symbolSize: (data: any) => {
          // 分数越高，粒子越大
          return 8 + (data[2] / 30) * 8;
        },
        itemStyle: {
          color: (params: any) => {
            // 分数越高，颜色越亮（从蓝色到绿色）
            const score = params.data[2];
            const ratio = score / 30;
            if (ratio > 0.7) return '#10b981'; // 绿色
            if (ratio > 0.4) return '#3b82f6'; // 蓝色
            return '#6366f1'; // 紫色
          },
          borderColor: '#fff',
          borderWidth: 1,
          shadowBlur: 10,
          shadowColor: 'rgba(99, 102, 241, 0.5)'
        },
        emphasis: {
          itemStyle: {
            shadowBlur: 20,
            shadowColor: 'rgba(16, 185, 129, 0.8)'
          }
        },
        animationDuration: 800,
        animationEasing: 'cubicOut'
      },
      // 最优点标记
      {
        type: 'scatter',
        data: [[70, 70, 30]],
        symbolSize: 15,
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
    animationDuration: 800,
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
        粒子群优化 · 第 {{ props.iteration || 1 }} 次迭代
      </span>
      <span class="text-slate-400">
        粒子聚拢中… 惯性 / 自我 / 群体
      </span>
    </div>
  </div>
</template>

<style scoped>
</style>
