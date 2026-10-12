<template>
  <div class="grid-heatmap-container">
    <div class="mb-4 flex items-center justify-between">
      <div>
        <h3 class="text-sm font-semibold text-slate-200">参数热力图</h3>
        <p class="text-xs text-slate-400">实时展示参数组合的性能分布</p>
      </div>
      <div class="flex items-center gap-3 text-xs text-slate-400">
        <div class="flex items-center gap-2">
          <span>低</span>
          <div class="flex h-3 w-24 gap-0.5 rounded overflow-hidden">
            <div class="flex-1 bg-slate-800"></div>
            <div class="flex-1 bg-slate-700"></div>
            <div class="flex-1 bg-amber-900/50"></div>
            <div class="flex-1 bg-amber-700/70"></div>
            <div class="flex-1 bg-emerald-600"></div>
          </div>
          <span>高</span>
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
          <p class="text-xs">等待网格搜索数据...</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick, computed } from 'vue';

interface HeatmapPoint {
  param1: number;
  param2: number;
  metric: number;
}

interface Props {
  data: Array<{ params: Record<string, any>; metric: number }>;
  paramKeys?: string[];
}

const props = withDefaults(defineProps<Props>(), {
  paramKeys: () => []
});

const chartContainer = ref<HTMLElement | null>(null);
const chartCanvas = ref<HTMLCanvasElement | null>(null);
let ctx: CanvasRenderingContext2D | null = null;
let animationFrame: number | null = null;

// 将数据转换为热力图点
const heatmapData = computed<HeatmapPoint[]>(() => {
  if (props.data.length === 0 || props.paramKeys.length < 2) return [];
  
  const key1 = props.paramKeys[0];
  const key2 = props.paramKeys[1];
  
  return props.data
    .filter(d => d.params && d.params[key1] !== undefined && d.params[key2] !== undefined)
    .map(d => ({
      param1: Number(d.params[key1]),
      param2: Number(d.params[key2]),
      metric: d.metric
    }));
});

// 获取颜色渐变
const getColor = (value: number, min: number, max: number): string => {
  if (max === min) return '#10b981';
  
  const normalized = (value - min) / (max - min);
  
  if (normalized < 0.2) {
    // 低值：深灰色
    const t = normalized / 0.2;
    return `rgb(${Math.round(30 + t * 20)}, ${Math.round(41 + t * 20)}, ${Math.round(59 + t * 20)})`;
  } else if (normalized < 0.5) {
    // 中低值：琥珀色
    const t = (normalized - 0.2) / 0.3;
    return `rgb(${Math.round(180 - t * 60)}, ${Math.round(83 - t * 30)}, ${Math.round(9 + t * 20)})`;
  } else {
    // 高值：绿色
    const t = (normalized - 0.5) / 0.5;
    return `rgb(${Math.round(16 + t * 20)}, ${Math.round(185 - t * 20)}, ${Math.round(129 - t * 10)})`;
  }
};

const drawChart = () => {
  if (!chartCanvas.value || !ctx || heatmapData.value.length === 0) return;
  
  const canvas = chartCanvas.value;
  const width = canvas.width;
  const height = canvas.height;
  
  // 清空画布
  ctx.clearRect(0, 0, width, height);
  
  const padding = { top: 20, right: 20, bottom: 40, left: 50 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  
  // 计算数据范围
  const param1Values = heatmapData.value.map(d => d.param1);
  const param2Values = heatmapData.value.map(d => d.param2);
  const metrics = heatmapData.value.map(d => d.metric);
  
  const minParam1 = Math.min(...param1Values);
  const maxParam1 = Math.max(...param1Values);
  const minParam2 = Math.min(...param2Values);
  const maxParam2 = Math.max(...param2Values);
  const minMetric = Math.min(...metrics);
  const maxMetric = Math.max(...metrics);
  
  // 创建网格
  const gridSize = 20;
  const cellWidth = chartWidth / gridSize;
  const cellHeight = chartHeight / gridSize;
  
  // 计算每个网格单元的平均指标值
  const grid: number[][] = Array(gridSize).fill(0).map(() => Array(gridSize).fill(NaN));
  const gridCounts: number[][] = Array(gridSize).fill(0).map(() => Array(gridSize).fill(0));
  
  heatmapData.value.forEach(point => {
    const xIdx = Math.min(
      gridSize - 1,
      Math.floor(((point.param1 - minParam1) / (maxParam1 - minParam1 || 1)) * gridSize)
    );
    const yIdx = Math.min(
      gridSize - 1,
      Math.floor(((point.param2 - minParam2) / (maxParam2 - minParam2 || 1)) * gridSize)
    );
    
    if (isNaN(grid[yIdx][xIdx])) {
      grid[yIdx][xIdx] = point.metric;
      gridCounts[yIdx][xIdx] = 1;
    } else {
      grid[yIdx][xIdx] = (grid[yIdx][xIdx] * gridCounts[yIdx][xIdx] + point.metric) / (gridCounts[yIdx][xIdx] + 1);
      gridCounts[yIdx][xIdx]++;
    }
  });
  
  // 绘制热力图
  for (let y = 0; y < gridSize; y++) {
    for (let x = 0; x < gridSize; x++) {
      if (!isNaN(grid[y][x])) {
        const color = getColor(grid[y][x], minMetric, maxMetric);
        ctx.fillStyle = color;
        ctx.fillRect(
          padding.left + x * cellWidth,
          padding.top + y * cellHeight,
          cellWidth,
          cellHeight
        );
      }
    }
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
  if (props.paramKeys.length > 0) {
    ctx.fillText(props.paramKeys[0], width / 2, height - padding.bottom + 8);
  }
  
  // Y轴标签
  ctx.save();
  ctx.translate(15, height / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = 'center';
  if (props.paramKeys.length > 1) {
    ctx.fillText(props.paramKeys[1], 0, 0);
  }
  ctx.restore();
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

watch(() => [props.data, props.paramKeys], () => {
  if (props.data.length > 0 && props.paramKeys.length >= 2) {
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
.grid-heatmap-container {
  position: relative;
  width: 100%;
}
</style>