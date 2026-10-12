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

// 生成温度曲线和分数曲线
const chartData = computed(() => {
  const progress = props.progress / 100;
  const iterations = 50;
  const data: Array<{ iteration: number; score: number; temperature: number }> = [];
  
  // 初始温度高，逐渐降低
  const initialTemp = 100;
  const finalTemp = 1;
  
  // 目标分数（逐渐接近）
  const targetScore = 30;
  const initialScore = 10;
  
  for (let i = 0; i <= iterations; i++) {
    const iterProgress = i / iterations;
    const currentProgress = Math.min(1, progress * (iterations / 50));
    
    if (iterProgress > currentProgress) break;
    
    // 温度曲线：指数衰减
    const temp = initialTemp * Math.exp(-iterProgress * 3) + finalTemp;
    
    // 分数曲线：高温时抖动大，低温时稳定
    const tempRatio = temp / initialTemp;
    const baseScore = initialScore + (targetScore - initialScore) * iterProgress;
    const noise = tempRatio * 5 * (Math.random() - 0.5); // 高温时噪声大
    const score = Math.max(0, baseScore + noise);
    
    data.push({
      iteration: i,
      score,
      temperature: temp
    });
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
  
  const data = chartData.value;
  const iterations = data.map(d => d.iteration);
  const scores = data.map(d => d.score);
  const temperatures = data.map(d => d.temperature);
  
  const option: EChartsOption = {
    backgroundColor: 'transparent',
    grid: [
      {
        top: '10%',
        right: '5%',
        bottom: '55%',
        left: '10%',
        containLabel: false
      },
      {
        top: '60%',
        right: '5%',
        bottom: '10%',
        left: '10%',
        containLabel: false
      }
    ],
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: '#334155',
      textStyle: {
        color: '#f1f5f9',
        fontSize: 12
      },
      formatter: (params: any) => {
        const p = params[0];
        if (!p) return '';
        const index = p.dataIndex;
        const item = data[index];
        return `
          <div style="padding: 8px;">
            <div style="font-weight: bold; margin-bottom: 4px;">迭代 ${item.iteration}</div>
            <div style="color: #6366f1;">当前得分: <span style="font-weight: bold;">${item.score.toFixed(2)}</span></div>
            <div style="color: #f472b6;">温度: <span style="font-weight: bold;">${item.temperature.toFixed(1)}</span></div>
          </div>
        `;
      }
    },
    xAxis: [
      {
        type: 'category',
        data: iterations,
        gridIndex: 0,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: '#94a3b8',
          fontSize: 10
        }
      },
      {
        type: 'category',
        data: iterations,
        gridIndex: 1,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: '#94a3b8',
          fontSize: 10
        }
      }
    ],
    yAxis: [
      {
        type: 'value',
        name: '分数',
        gridIndex: 0,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: '#94a3b8',
          fontSize: 10
        },
        nameTextStyle: {
          color: '#94a3b8',
          fontSize: 10
        },
        splitLine: {
          show: true,
          lineStyle: {
            color: 'rgba(148, 163, 184, 0.1)',
            type: 'dashed'
          }
        }
      },
      {
        type: 'value',
        name: '温度',
        gridIndex: 1,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: '#94a3b8',
          fontSize: 10
        },
        nameTextStyle: {
          color: '#94a3b8',
          fontSize: 10
        },
        splitLine: {
          show: true,
          lineStyle: {
            color: 'rgba(148, 163, 184, 0.1)',
            type: 'dashed'
          }
        }
      }
    ],
    series: [
      // 分数曲线（上图）
      {
        name: '当前得分',
        type: 'line',
        data: scores,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: false, // 不平滑，显示抖动
        lineStyle: {
          color: '#6366f1',
          width: 2
        },
        itemStyle: {
          color: '#6366f1'
        },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(99, 102, 241, 0.3)' },
            { offset: 1, color: 'rgba(99, 102, 241, 0)' }
          ])
        },
        symbol: 'circle',
        symbolSize: 4,
        animationDuration: 600,
        animationEasing: 'cubicOut'
      },
      // 温度曲线（下图，颜色从红到蓝）
      {
        name: '温度',
        type: 'line',
        data: temperatures.map((temp, idx) => [idx, temp]),
        xAxisIndex: 1,
        yAxisIndex: 1,
        smooth: true,
        lineStyle: {
          width: 3,
          color: (params: any) => {
            // 温度高时红色，温度低时蓝色
            const maxTemp = 100;
            const ratio = params.data[1] / maxTemp;
            if (ratio > 0.7) return '#ef4444'; // 红色
            if (ratio > 0.4) return '#f97316'; // 橙色
            if (ratio > 0.2) return '#eab308'; // 黄色
            return '#3b82f6'; // 蓝色
          }
        },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(239, 68, 68, 0.4)' },
            { offset: 0.5, color: 'rgba(234, 179, 8, 0.2)' },
            { offset: 1, color: 'rgba(59, 130, 246, 0.1)' }
          ])
        },
        symbol: 'none',
        animationDuration: 600,
        animationEasing: 'cubicOut'
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
        模拟退火 · 第 {{ props.iteration || 1 }} 次迭代
      </span>
      <span class="text-slate-400">
        温度: {{ (100 - props.progress).toFixed(1) }} → 冷却中
      </span>
    </div>
  </div>
</template>

<style scoped>
</style>
