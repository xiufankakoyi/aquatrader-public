<template>
  <div class="bg-[#151925] rounded-lg p-4 border border-slate-800">
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center space-x-2">
        <h2 class="text-lg font-semibold text-white">情感趋势分析</h2>
        <div class="group relative">
          <i class="fas fa-info-circle text-slate-400 cursor-help"></i>
          <div class="absolute bottom-6 left-1/2 transform -translate-x-1/2 bg-slate-800 text-xs text-slate-200 px-3 py-2 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none whitespace-nowrap z-10">
            时间序列情感分析：横轴为时间，纵轴为情感得分，柱状图为发帖数量
          </div>
        </div>
      </div>
      <div class="text-sm text-slate-400">
        基于 SnowNLP 情感分析
      </div>
    </div>
    <div ref="chartContainer" class="h-80"></div>
    <div v-if="loading" class="flex items-center justify-center h-80 text-slate-400">
      <i class="fas fa-spinner fa-spin mr-2"></i>
      正在加载情感趋势数据...
    </div>
    <div v-if="error" class="text-red-400 text-sm mt-2">
      {{ error }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import { fetchSentimentTrend, type SentimentTrendPoint } from '../api/backtestApi';

interface Props {
  selectedSymbol?: string;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const loading = ref(false);
const error = ref<string | null>(null);
const data = ref<SentimentTrendPoint[]>([]);

async function loadData() {
  // 1. 先清空数据，防止闪烁旧数据
  data.value = [];
  error.value = null;
  
  // 🔴 关键点：如果图表还在，先显示 loading 效果或清空
  if (chartInstance) {
    chartInstance.showLoading({
      text: '加载中...',
      color: '#3b82f6',
      textColor: '#94a3b8',
      maskColor: 'rgba(21, 25, 37, 0.8)',
    });
  }

  loading.value = true;
  
  try {
    const result = await fetchSentimentTrend(props.selectedSymbol, 30);
    
    // 🔴 核心修复：强制按日期升序排序！
    // 防止后端返回乱序导致折线图"反复横跳"
    data.value = result.sort((a, b) => {
      return new Date(a.date).getTime() - new Date(b.date).getTime();
    });

    await nextTick();
    updateChart();
  } catch (e) {
    console.error('获取情感趋势数据失败:', e);
    error.value = '获取数据失败，请检查后端 API';
    if (chartInstance) chartInstance.hideLoading();
  } finally {
    loading.value = false;
    if (chartInstance) chartInstance.hideLoading();
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

  chartInstance = echarts.init(chartContainer.value);
  updateChart();
}

function updateChart() {
  if (!chartInstance) return;

  // 🔴 修复：如果数据为空，清空画布并退出，不要留着旧图
  if (!data.value || data.value.length === 0) {
    chartInstance.clear();
    return;
  }

  // 提取时间、情感得分和发帖数量数据
  const timeLabels = data.value.map(item => item.date);
  // 使用接口返回的平均情感值字段 avg_sentiment
  const sentimentScores = data.value.map(item => item.avg_sentiment);
  const postCounts = data.value.map(item => item.post_count);

  // 计算日期间隔，避免标签重叠
  const labelInterval = Math.max(1, Math.floor(timeLabels.length / 8));

  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross',
        label: {
          backgroundColor: '#6a7985'
        }
      },
      formatter: function (params: any) {
        if (!Array.isArray(params) || params.length === 0) {
          return '';
        }

        let result = `${params[0].name}<br/>`;

        params.forEach((param: any) => {
          // ECharts 在部分图表中会把 value 作为数组，这里做一下兼容处理
          const rawValue = Array.isArray(param.value)
            ? (param.value[1] ?? param.value[0])
            : param.value;

          if (param.seriesName === '情感得分') {
            const num = Number(rawValue);
            const display = Number.isFinite(num) ? num.toFixed(3) : '-';
            result += `${param.seriesName}: ${display}<br/>`;
          } else {
            const display = rawValue ?? '-';
            result += `${param.seriesName}: ${display}<br/>`;
          }
        });

        return result;
      }
    },
    legend: {
      data: ['情感得分', '发帖数量'],
      textStyle: {
        color: '#94a3b8'
      }
    },
    grid: {
      top: '15%',
      left: '5%',
      right: '5%',
      bottom: '10%',
      containLabel: true
    },
    xAxis: [
      {
        type: 'category',
        boundaryGap: false,
        data: timeLabels,
        axisLine: {
          lineStyle: {
            color: '#475569'
          }
        },
        axisLabel: {
          color: '#94a3b8',
          interval: labelInterval, // 设置日期间隔，避免标签重叠
          rotate: 45, // 旋转45度，进一步避免重叠
          formatter: (value: string) => {
            // 格式化日期显示，只显示月-日
            if (value && value.length >= 10) {
              return value.substring(5, 10);
            }
            return value;
          }
        }
      }
    ],
    yAxis: [
      {
        type: 'value',
        name: '情感得分',
        position: 'left',
        min: -1,
        max: 1,
        interval: 0.5,
        axisLine: {
          lineStyle: {
            color: '#475569'
          }
        },
        axisLabel: {
          color: '#94a3b8',
          formatter: function (value: number) {
            if (value === 1) return '极度看多';
            if (value === -1) return '极度看空';
            return value;
          }
        },
        splitLine: {
          show: true,
          lineStyle: {
            type: 'dashed',
            color: '#1e293b',
            width: 1
          }
        }
      },
      {
        type: 'value',
        name: '发帖数量',
        position: 'right',
        axisLine: {
          lineStyle: {
            color: '#475569'
          }
        },
        axisLabel: {
          color: '#94a3b8',
          formatter: '{value}'
        }
      }
    ],
    series: [
      {
        name: '情感得分',
        type: 'line',
        yAxisIndex: 0,
        smooth: 0.5, // 增加平滑度，从0.3改为0.5
        data: sentimentScores,
        lineStyle: {
          width: 3,
          color: '#3b82f6'
        },
        markLine: {
          data: [{ yAxis: 0 }],
          lineStyle: {
            color: '#64748b', // 更明显的颜色
            opacity: 0.8, // 增加不透明度
            width: 2, // 加粗
            type: 'solid' // 改为实线
          },
          label: {
            show: true,
            position: 'end',
            formatter: '0',
            color: '#94a3b8'
          }
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              {
                offset: 0,
                color: 'rgba(59, 130, 246, 0.3)'
              },
              {
                offset: 1,
                color: 'rgba(59, 130, 246, 0.05)'
              }
            ]
          }
        }
      },
      {
        name: '发帖数量',
        type: 'bar',
        yAxisIndex: 1,
        data: postCounts,
        itemStyle: {
          // 降低绿色饱和度，使用更柔和的绿色
          color: 'rgba(74, 222, 128, 0.4)' // 从高饱和度的绿色改为更柔和的绿色
        },
        barWidth: '60%' // 减小柱子宽度，避免过于拥挤
      }
    ]
  };

  // 🔴 核心修复：
  // 1. 先 clear() 清除所有之前的状态（包括缩放、残留的线）
  chartInstance.clear(); 
  // 2. 使用 setOption(option, true) 强制不合并
  chartInstance.setOption(option, true);
}

// 定义一个 resize 处理函数
const handleResize = () => {
  chartInstance?.resize();
};

onMounted(() => {
  // 1. 先初始化图表容器（此时是空的）
  initChart();
  
  // 2. 添加监听
  window.addEventListener('resize', handleResize);
  
  // 3. 最后加载数据
  // 注意：不需要在这里写 nextTick，因为 initChart 已经建立好实例了
  // loadData 会触发 data 变化，data 变化会触发 watch，watch 会触发 updateChart
  if (props.selectedSymbol) {
    loadData();
  }
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  // 🔴 必须销毁实例，防止内存泄漏和重建时的冲突
  if (chartInstance) {
    chartInstance.dispose();
    chartInstance = null;
  }
});

watch(
  () => data.value,
  () => {
    nextTick(() => updateChart());
  },
  { deep: true }
);

watch(
  () => props.selectedSymbol,
  (newSymbol, oldSymbol) => {
    // 当股票符号改变时（包括从有到无，或从无到有，或从一只股票切换到另一只），都重新加载数据
    if (newSymbol !== oldSymbol) {
      loadData();
    }
  }
);
</script>