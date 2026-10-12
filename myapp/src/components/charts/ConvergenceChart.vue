<template>
  <div class="convergence-chart-container">
    <div class="mb-4 flex items-center justify-between">
      <div>
        <h3 class="text-sm font-semibold text-slate-200">收敛曲线</h3>
        <p class="text-xs text-slate-400">实时追踪优化进程，观察算法收敛趋势</p>
      </div>
      <div class="flex items-center gap-2 text-xs text-slate-400">
        <div class="flex items-center gap-1">
          <div class="h-2 w-2 rounded-full bg-indigo-400"></div>
          <span>当前值</span>
        </div>
        <div class="flex items-center gap-1">
          <div class="h-2 w-2 rounded-full bg-emerald-400"></div>
          <span>最佳值</span>
        </div>
      </div>
    </div>
    
    <div ref="chartContainer" class="relative h-64 w-full rounded-xl bg-slate-950/50 p-4">
      <canvas ref="chartCanvas"></canvas>
      <div v-if="data.length === 0" class="absolute inset-0 flex items-center justify-center">
        <div class="text-center text-slate-500">
          <svg class="mx-auto h-12 w-12 mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          <p class="text-xs">等待优化数据...</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue';

interface Props {
  data: Array<{ iteration: number; metric: number }>;
  metricLabel?: string;
}

const props = withDefaults(defineProps<Props>(), {
  metricLabel: '指标值'
});

const chartContainer = ref<HTMLElement | null>(null);
const chartCanvas = ref<HTMLCanvasElement | null>(null);
let ctx: CanvasRenderingContext2D | null = null;
let animationFrame: number | null = null;

const drawChart = () => {
  if (!chartCanvas.value || !ctx || props.data.length === 0) return;
  
  const canvas = chartCanvas.value;
  const width = canvas.width;
  const height = canvas.height;
  
  // 清空画布
  ctx.clearRect(0, 0, width, height);
  
  // 设置样式
  const padding = { top: 20, right: 20, bottom: 40, left: 50 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  
  // 计算数据范围
  const metrics = props.data.map(d => d.metric);
  const iterations = props.data.map(d => d.iteration);
  const minMetric = Math.min(...metrics);
  const maxMetric = Math.max(...metrics);
  const minIter = Math.min(...iterations);
  const maxIter = Math.max(...iterations);
  
  const metricRange = maxMetric - minMetric || 1;
  const iterRange = maxIter - minIter || 1;
  
  // 绘制网格
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.1)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) {
    const y = padding.top + (chartHeight / 5) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
  }
  
  // 绘制坐标轴
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.3)';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, height - padding.bottom);
  ctx.lineTo(width - padding.right, height - padding.bottom);
  ctx.stroke();
  
  // 绘制标签
  ctx.fillStyle = 'rgba(148, 163, 184, 0.6)';
  ctx.font = '11px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  
  // X轴标签
  for (let i = 0; i <= 5; i++) {
    const iter = minIter + (iterRange / 5) * i;
    const x = padding.left + (chartWidth / 5) * i;
    ctx.fillText(Math.round(iter).toString(), x, height - padding.bottom + 8);
  }
  
  // Y轴标签
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 5; i++) {
    const metric = maxMetric - (metricRange / 5) * i;
    const y = padding.top + (chartHeight / 5) * i;
    ctx.fillText(metric.toFixed(2), padding.left - 8, y);
  }
  
  if (props.data.length < 2) return;
  
  // 绘制最佳值曲线（绿色，平滑）
  ctx.strokeStyle = '#10b981';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  let bestSoFar = props.data[0].metric;
  const bestPoints: Array<{ x: number; y: number }> = [];
  
  props.data.forEach((point, idx) => {
    if (point.metric > bestSoFar) {
      bestSoFar = point.metric;
    }
    const x = padding.left + ((point.iteration - minIter) / iterRange) * chartWidth;
    const y = padding.top + chartHeight - ((bestSoFar - minMetric) / metricRange) * chartHeight;
    bestPoints.push({ x, y });
    
    if (idx === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  
  // 绘制当前值曲线（蓝色，带点）
  ctx.strokeStyle = '#818cf8';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([5, 5]);
  ctx.beginPath();
  props.data.forEach((point, idx) => {
    const x = padding.left + ((point.iteration - minIter) / iterRange) * chartWidth;
    const y = padding.top + chartHeight - ((point.metric - minMetric) / metricRange) * chartHeight;
    
    if (idx === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  ctx.setLineDash([]);
  
  // 绘制数据点
  let currentBest = props.data[0].metric;
  props.data.forEach((point) => {
    const x = padding.left + ((point.iteration - minIter) / iterRange) * chartWidth;
    const y = padding.top + chartHeight - ((point.metric - minMetric) / metricRange) * chartHeight;
    
    // 当前值点
    ctx.fillStyle = '#818cf8';
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
    
    // 最佳值点（如果是最佳）
    if (point.metric >= currentBest) {
      currentBest = point.metric;
      ctx.fillStyle = '#10b981';
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();
    }
  });
};

const resizeCanvas = () => {
  if (!chartCanvas.value || !chartContainer.value) return;
  
  const rect = chartContainer.value.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  
  chartCanvas.value.width = rect.width * dpr;
  chartCanvas.value.height = rect.height * dpr;
  chartCanvas.value.style.width = `${rect.width}px`;
  chartCanvas.value.style.height = `${rect.height}px`;
  
  if (ctx) {
    ctx.scale(dpr, dpr);
  }
  
  drawChart();
};

const animate = () => {
  drawChart();
  animationFrame = requestAnimationFrame(animate);
};

watch(() => props.data, () => {
  if (props.data.length > 0) {
    nextTick(() => {
      drawChart();
    });
  }
}, { deep: true });

onMounted(() => {
  if (chartCanvas.value) {
    ctx = chartCanvas.value.getContext('2d');
    resizeCanvas();
    animate();
    
    window.addEventListener('resize', resizeCanvas);
  }
});

onUnmounted(() => {
  if (animationFrame) {
    cancelAnimationFrame(animationFrame);
  }
  window.removeEventListener('resize', resizeCanvas);
});
</script>

<style scoped>
.convergence-chart-container {
  position: relative;
  width: 100%;
}
</style>