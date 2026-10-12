<template>
  <div class="bg-[#151925] rounded-lg p-4 border border-slate-800">
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center space-x-2">
        <h2 class="text-lg font-semibold text-white">LDA 主题分布</h2>
        <div class="group relative">
          <i class="fas fa-info-circle text-slate-400 cursor-help"></i>
          <div class="absolute bottom-6 left-1/2 transform -translate-x-1/2 bg-slate-800 text-xs text-slate-200 px-3 py-2 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none whitespace-nowrap z-10">
            基于 Latent Dirichlet Allocation 主题模型分析
          </div>
        </div>
      </div>
      <div class="text-sm text-slate-400">
        基于 LDA 主题模型算法
      </div>
    </div>
    <div ref="chartContainer" class="h-80"></div>
    <div v-if="loading" class="flex items-center justify-center h-80 text-slate-400">
      <i class="fas fa-spinner fa-spin mr-2"></i>
      正在加载主题分布数据...
    </div>
    <div v-if="error" class="text-red-400 text-sm mt-2">
      {{ error }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import { fetchLdaTopics, type LdaTopicData } from '../api/backtestApi';

interface Props {
  selectedSymbol?: string;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const loading = ref(false);
const error = ref<string | null>(null);
const data = ref<LdaTopicData | null>(null);

async function loadData() {
  // 第一步：清空当前数据
  data.value = null;
  error.value = null;
  
  // 第二步：显示加载状态
  loading.value = true;
  
  try {
    // 第三步：获取新数据
    const result = await fetchLdaTopics(props.selectedSymbol);
    data.value = result;
    await nextTick();
    updateChart();
  } catch (e) {
    console.error('获取 LDA 主题分布数据失败:', e);
    error.value = '获取数据失败，请检查后端 API';
  } finally {
    loading.value = false;
  }
}

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
  if (!chartInstance) return;
  
  // 确保 data.value 是有效的 LdaTopicData 对象
  const topicData = data.value || { topics: [], scores: [] };
  if (!topicData.topics || !topicData.scores) return;

  // 计算最大分数，用于生成渐变色
  const maxScore = Math.max(...topicData.scores);
  
  // 使用渐变色表示权重，权重越高颜色越深/越亮
  const getColorByWeight = (score: number, maxScore: number) => {
    const ratio = score / maxScore;
    // 使用蓝色系渐变，权重越高颜色越深
    // 从浅蓝 (#60a5fa) 到深蓝 (#3b82f6) 再到紫色 (#6366f1)
    if (ratio > 0.8) {
      return '#6366f1'; // 最高权重：紫色
    } else if (ratio > 0.6) {
      return '#3b82f6'; // 高权重：深蓝
    } else if (ratio > 0.4) {
      return '#60a5fa'; // 中权重：中蓝
    } else if (ratio > 0.2) {
      return '#93c5fd'; // 低权重：浅蓝
    } else {
      return '#cbd5e1'; // 最低权重：灰蓝
    }
  };

  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow'
      },
      formatter: (params: any) => {
        const data = params[0];
        return `${data.name}<br/>分布权重: ${data.value.toFixed(3)}`;
      }
    },
    grid: {
      left: '25%', // 增加左边距，为长文本留出空间
      right: '8%', // 增加右边距，为数字标签留出空间
      bottom: '3%',
      top: '5%',
      containLabel: false // 改为false，手动控制间距
    },
    xAxis: {
      type: 'value',
      axisLine: {
        lineStyle: {
          color: '#475569'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        formatter: '{value}'
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: '#1e293b',
          type: 'dashed'
        }
      }
    },
    yAxis: {
      type: 'category',
      data: topicData.topics,
      axisLine: {
        lineStyle: {
          color: '#475569'
        }
      },
      axisLabel: {
        color: '#94a3b8',
        width: 120, // 设置标签宽度，确保文字完整显示
        overflow: 'truncate', // 超出部分截断
        formatter: (value: string) => {
          // 如果文字太长，截断并加省略号
          if (value && value.length > 8) {
            return value.substring(0, 8) + '...';
          }
          return value;
        }
      }
    },
    series: [
      {
        type: 'bar',
        data: topicData.scores.map((score, index) => ({
          value: score,
          itemStyle: {
            color: getColorByWeight(score, maxScore) // 使用渐变色表示权重
          }
        })),
        barWidth: '35%', // 减小柱子宽度，从60%改为35%，避免过于粗重
        barCategoryGap: '30%', // 增加柱子之间的间距
        label: {
          show: true,
          position: 'right',
          color: '#94a3b8',
          formatter: (params: any) => {
            // 格式化数字，保留3位小数
            return params.value.toFixed(3);
          },
          distance: 5 // 数字标签与柱子之间的距离
        }
      }
    ]
  };

  chartInstance.setOption(option);
}

function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
  loadData();
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

watch(
  () => data.value,
  () => {
    nextTick(() => updateChart());
  },
  { deep: true }
);

// 当选择的股票符号改变时，重新加载数据
watch(
  () => props.selectedSymbol,
  () => {
    if (props.selectedSymbol) {
      loadData();
    }
  }
);
</script>