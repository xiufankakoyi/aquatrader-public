<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

const props = defineProps<{
  /** 0-100 的整体进度，用来微调动画节奏 */
  progress: number;
  /** 当前代数，用于文案展示 */
  iteration: number;
  /** 历史数据：每代的所有个体分数 */
  history?: Array<{ iteration: number; metric: number; params?: Record<string, any> }>;
}>();

const chartContainer = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 检查分数是否为有效值（非惩罚值）
const isValidScore = (score: number): boolean => {
  return score > -10000; // 惩罚值通常是-1e9
};

// 按代数分组数据，用于箱线图
const boxplotData = computed(() => {
  if (!props.history || props.history.length === 0) {
    return { generations: [], data: [] };
  }
  
  // 按iteration分组，并过滤惩罚值
  const byGeneration = new Map<number, number[]>();
  props.history.forEach(item => {
    if (!isValidScore(item.metric)) return; // 跳过惩罚值
    if (!byGeneration.has(item.iteration)) {
      byGeneration.set(item.iteration, []);
    }
    byGeneration.get(item.iteration)!.push(item.metric);
  });
  
  // 如果所有数据都是惩罚值，返回空
  if (byGeneration.size === 0) {
    return { generations: [], data: [] };
  }
  
  // 转换为箱线图数据格式：[min, Q1, median, Q3, max]
  const generations = Array.from(byGeneration.keys()).sort((a, b) => a - b);
  const result: Array<[number, number, number, number, number]> = [];
  
  generations.forEach(gen => {
    const scores = byGeneration.get(gen)!;
    scores.sort((a, b) => a - b);
    
    const min = scores[0];
    const max = scores[scores.length - 1];
    const q1Index = Math.floor(scores.length * 0.25);
    const medianIndex = Math.floor(scores.length * 0.5);
    const q3Index = Math.floor(scores.length * 0.75);
    
    const q1 = scores[q1Index];
    const median = scores[medianIndex];
    const q3 = scores[q3Index];
    
    result.push([min, q1, median, q3, max]);
  });
  
  return {
    generations,
    data: result
  };
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
  
  const { generations, data } = boxplotData.value;
  
  if (data.length === 0) {
    // 检查是否所有数据都是惩罚值
    const hasAnyData = props.history && props.history.length > 0;
    const allInvalid = hasAnyData && props.history!.every(item => !isValidScore(item.metric));
    
    chartInstance.setOption({
      title: {
        text: allInvalid ? '策略在训练集中无交易' : '等待数据...',
        left: 'center',
        top: 'middle',
        textStyle: {
          color: allInvalid ? '#ef4444' : '#94a3b8',
          fontSize: 14
        }
      }
    });
    return;
  }
  
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
        const gen = generations[params.dataIndex];
        const [min, q1, median, q3, max] = params.data;
        return `
          <div style="padding: 8px;">
            <div style="font-weight: bold; margin-bottom: 6px;">第 ${gen} 代</div>
            <div style="font-size: 11px; line-height: 1.6;">
              <div>最小值: <span style="color: #94a3b8;">${min.toFixed(4)}</span></div>
              <div>Q1: <span style="color: #94a3b8;">${q1.toFixed(4)}</span></div>
              <div>中位数: <span style="color: #10b981; font-weight: bold;">${median.toFixed(4)}</span></div>
              <div>Q3: <span style="color: #94a3b8;">${q3.toFixed(4)}</span></div>
              <div>最大值: <span style="color: #94a3b8;">${max.toFixed(4)}</span></div>
            </div>
          </div>
        `;
}
    },
    xAxis: {
      type: 'category',
      name: '代数 (Generation)',
      data: generations.map(g => `第${g}代`),
      axisLine: {
        lineStyle: {
          color: '#334155'
  }
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 10
      },
      nameTextStyle: {
        color: '#94a3b8',
        fontSize: 11
      }
    },
    yAxis: {
      type: 'value',
      name: '分数',
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 10
      },
      nameTextStyle: {
        color: '#94a3b8',
        fontSize: 11
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: 'rgba(148, 163, 184, 0.1)',
          type: 'dashed'
  }
}
    },
    series: [{
      name: '分数分布',
      type: 'boxplot',
      data: data,
      itemStyle: {
        color: '#6366f1',
        borderColor: '#8b5cf6',
        borderWidth: 2
      },
      emphasis: {
        itemStyle: {
          borderColor: '#a855f7',
          borderWidth: 3
        }
      },
      animationDuration: 500,
      animationEasing: 'cubicOut'
    }]
  };
  
  chartInstance.setOption(option, { notMerge: true, lazyUpdate: false });
}

watch(() => [props.history, props.iteration], () => {
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
    
    <div v-if="!history?.length" class="absolute inset-0 flex items-center justify-center text-slate-500 text-xs">
      等待数据...
    </div>

    <div class="absolute inset-x-0 bottom-2 px-3 text-[11px] text-slate-300 flex justify-between pointer-events-none">
      <span>
        遗传算法 · 第 {{ props.iteration || 1 }} 代
      </span>
      <span class="text-slate-400">
        箱体越高 → 分数越好 | 箱体越窄 → 收敛越快
      </span>
    </div>
  </div>
</template>

<style scoped>
</style>
