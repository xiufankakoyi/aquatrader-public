<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

const props = defineProps<{
  progress: number;
  iteration?: number;
  totalIterations?: number;
  bestScore?: number | null;
  history?: Array<{ iteration: number; metric: number; params?: Record<string, any> }>;
}>();

const chartContainer = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 检查分数是否为有效值（非惩罚值）
const isValidScore = (score: number): boolean => {
  return score > -10000; // 惩罚值通常是-1e9
};

// 生成置信区间数据（呼吸感效果）
const confidenceData = computed(() => {
  const historyData = (props.history || []).filter(h => isValidScore(h.metric)); // 过滤惩罚值
  const iterations = historyData.map(h => h.iteration);
  const scores = historyData.map(h => h.metric);
  
  if (iterations.length === 0) {
    // 生成模拟数据
    const mockIterations: number[] = [];
    const mockMeans: number[] = [];
    const mockUpper: number[] = [];
    const mockLower: number[] = [];
    const mockSamples: Array<[number, number]> = [];
    
    const progress = props.progress / 100;
    const count = Math.floor(20 * progress);
    
    for (let i = 0; i <= count; i++) {
      mockIterations.push(i);
      const mean = 10 + (i / 20) * 20; // 均值逐渐上升
      const uncertainty = 5 * Math.exp(-i * 0.1); // 不确定性逐渐减小
      
      mockMeans.push(mean);
      mockUpper.push(mean + uncertainty);
      mockLower.push(mean - uncertainty);
      
      // 采样点（红点）
      if (i % 3 === 0) {
        mockSamples.push([i, mean + (Math.random() - 0.5) * uncertainty]);
      }
    }
    
    return {
      iterations: mockIterations,
      means: mockMeans,
      upper: mockUpper,
      lower: mockLower,
      samples: mockSamples
    };
  }
  
  // 使用真实数据
  const means = scores;
  const upper: number[] = [];
  const lower: number[] = [];
  const samples: Array<[number, number]> = [];
  
  scores.forEach((score, idx) => {
    // 置信区间宽度：新采样点附近窄，其他地方宽（呼吸感）
    const isNearSample = idx === scores.length - 1 || idx % 3 === 0;
    const baseUncertainty = 3;
    const uncertainty = isNearSample ? baseUncertainty * 0.3 : baseUncertainty;
    
    upper.push(score + uncertainty);
    lower.push(score - uncertainty);
    
    // 标记采样点
    if (isNearSample) {
      samples.push([iterations[idx], score]);
    }
  });
  
  return {
    iterations,
    means,
    upper,
    lower,
    samples
  };
});

function initChart() {
  if (!chartContainer.value) return;
  
  chartInstance = echarts.init(chartContainer.value);
  updateChart();
  
  window.addEventListener('resize', handleResize);
  
  // 呼吸感动画：定期更新置信区间
  const animationInterval = setInterval(() => {
    if (chartInstance) {
      updateChart();
    }
  }, 2000);
  
  // 清理定时器
  onUnmounted(() => {
    clearInterval(animationInterval);
  });
}

function handleResize() {
  chartInstance?.resize();
}

function updateChart() {
  if (!chartInstance) return;

  const { iterations, means, upper, lower, samples } = confidenceData.value;
  
  // 如果所有数据都是惩罚值，显示空状态
  if (iterations.length === 0) {
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
  
  // 呼吸感：置信区间宽度动态变化
  const breathPhase = (Date.now() / 2000) % (2 * Math.PI);
  const breathFactor = 1 + Math.sin(breathPhase) * 0.1; // 10%的呼吸幅度
  
  const adjustedUpper = upper.map((u, i) => {
    const mean = means[i];
    const baseUncertainty = u - mean;
    return mean + baseUncertainty * breathFactor;
  });
  
  const adjustedLower = lower.map((l, i) => {
    const mean = means[i];
    const baseUncertainty = mean - l;
    return mean - baseUncertainty * breathFactor;
  });

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
        return `
          <div style="padding: 8px;">
            <div style="font-weight: bold; margin-bottom: 4px;">迭代 ${iterations[index]}</div>
            <div style="color: #6366f1;">预测均值: <span style="font-weight: bold;">${means[index].toFixed(2)}</span></div>
            <div style="color: #94a3b8; font-size: 10px;">
              置信区间: [${adjustedLower[index].toFixed(2)}, ${adjustedUpper[index].toFixed(2)}]
            </div>
          </div>
        `;
      }
    },
    xAxis: {
      type: 'category',
      data: iterations,
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 10
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
    series: [
      // 置信区间（阴影区域）
      {
        name: '置信区间',
        type: 'line',
        data: adjustedUpper,
        lineStyle: {
          opacity: 0
        },
        stack: 'confidence',
        symbol: 'none',
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(99, 102, 241, 0.3)' },
            { offset: 1, color: 'rgba(99, 102, 241, 0)' }
          ])
        },
        animationDuration: 2000,
        animationEasing: 'sineOut'
      },
      {
        name: '置信区间',
        type: 'line',
        data: adjustedLower,
        lineStyle: {
          opacity: 0
        },
        stack: 'confidence',
        symbol: 'none',
        areaStyle: {
          color: 'rgba(15, 23, 42, 0.8)' // 背景色，形成阴影效果
        },
        animationDuration: 2000,
        animationEasing: 'sineOut'
      },
      // 预测均值（实线）
      {
        name: '预测均值',
        type: 'line',
        data: means,
        smooth: true,
        lineStyle: {
          color: '#6366f1',
          width: 2
        },
        symbol: 'none',
        animationDuration: 500,
        animationEasing: 'cubicOut'
      },
      // 采样点（红点，采样时附近置信区间收缩）
      {
        name: '采样点',
        type: 'scatter',
        data: samples,
        symbolSize: 10,
        itemStyle: {
          color: '#ef4444',
          borderColor: '#fff',
          borderWidth: 2,
          shadowBlur: 15,
          shadowColor: 'rgba(239, 68, 68, 0.8)'
        },
        z: 10,
        animationDuration: 500,
        animationEasing: 'elasticOut'
      }
    ],
    animationDuration: 2000,
    animationEasing: 'sineOut'
  };

  chartInstance.setOption(option, { notMerge: true, lazyUpdate: false });
}

watch(() => [props.history, props.progress], () => {
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
        贝叶斯优化 · 第 {{ props.iteration || 0 }}
        <span v-if="props.totalIterations" class="text-slate-500">/{{ props.totalIterations }}</span>
        次采样
      </span>
      <span class="text-slate-400">
        最佳 {{ props.bestScore !== null && props.bestScore !== undefined && isValidScore(props.bestScore) ? props.bestScore.toFixed(4) : '无效' }} · 置信区间收缩中
      </span>
    </div>
  </div>
</template>

<style scoped>
</style>
