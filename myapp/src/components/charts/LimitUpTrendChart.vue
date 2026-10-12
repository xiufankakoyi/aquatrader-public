<template>
  <div class="limit-up-trend-chart h-full flex flex-col">
    <!-- 图表容器 - 自适应父容器高度 -->
    <div ref="chartRef" class="flex-1 w-full min-h-0"></div>
    
    <!-- 加载状态 -->
    <div v-if="loading" class="absolute inset-0 flex items-center justify-center bg-slate-900/50 rounded-xl">
      <div class="flex items-center gap-2 text-slate-400">
        <i class="fas fa-spinner fa-spin"></i>
        <span>加载中...</span>
      </div>
    </div>
    
    <!-- 空状态 -->
    <div v-if="!loading && isEmpty" class="absolute inset-0 flex items-center justify-center">
      <div class="text-center text-slate-500">
        <i class="fas fa-chart-area text-4xl mb-2"></i>
        <p class="text-sm">暂无数据</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';

/**
 * LimitUpTrendChart - 涨停趋势图组件
 * 
 * 展示指定日期范围内的涨停趋势数据，包含：
 * - 涨停数量趋势（柱状图）
 * - 最高连板数（折线图）
 * - 炸板率（折线图，右轴）
 */

interface TrendData {
  dates: string[];
  limit_up_counts: number[];
  max_heights: number[];
  broken_ratios: number[];
  limit_down_counts: number[];
}

interface Props {
  data: TrendData | null;
  loading?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  data: null,
  loading: false
});

const chartRef = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const isEmpty = ref(true);

/**
 * 初始化图表
 */
const initChart = () => {
  if (!chartRef.value) return;
  
  chartInstance = echarts.init(chartRef.value);
  
  // 监听窗口大小变化
  const resizeHandler = () => chartInstance?.resize();
  window.addEventListener('resize', resizeHandler);
};

/**
 * 更新图表数据
 */
const updateChart = () => {
  if (!chartInstance || !props.data) {
    isEmpty.value = true;
    return;
  }
  
  const { dates, limit_up_counts, max_heights, broken_ratios } = props.data;
  
  if (!dates || dates.length === 0) {
    isEmpty.value = true;
    chartInstance.clear();
    return;
  }
  
  isEmpty.value = false;
  
  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      },
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: '#334155',
      textStyle: {
        color: '#e2e8f0',
        fontSize: 12
      },
      formatter: (params: any) => {
        const date = params[0].axisValue;
        let html = `<div class="font-medium mb-1">${date}</div>`;
        
        params.forEach((item: any) => {
          const value = item.seriesName === '炸板率' 
            ? `${(item.value * 100).toFixed(1)}%`
            : item.value;
          html += `<div class="flex items-center gap-2">
            <span style="background:${item.color}" class="w-2 h-2 rounded-full"></span>
            <span>${item.seriesName}: ${value}</span>
          </div>`;
        });
        
        return html;
      }
    },
    legend: {
      show: false
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: '10%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: dates.map(d => d.slice(5)), // 只显示月-日
      axisLine: {
        lineStyle: { color: '#334155' }
      },
      axisLabel: {
        color: '#64748b',
        fontSize: 10,
        rotate: 45
      },
      axisTick: {
        show: false
      }
    },
    yAxis: [
      {
        type: 'value',
        name: '数量/连板',
        min: 0,
        axisLine: {
          show: false
        },
        axisLabel: {
          color: '#64748b',
          fontSize: 10
        },
        splitLine: {
          lineStyle: { color: '#1e293b' }
        }
      },
      {
        type: 'value',
        name: '炸板率',
        min: 0,
        max: 1,
        axisLine: {
          show: false
        },
        axisLabel: {
          color: '#64748b',
          fontSize: 10,
          formatter: (value: number) => `${(value * 100).toFixed(0)}%`
        },
        splitLine: {
          show: false
        }
      }
    ],
    series: [
      {
        name: '涨停数量',
        type: 'bar',
        data: limit_up_counts,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#ef4444' },
            { offset: 1, color: '#dc2626' }
          ]),
          borderRadius: [2, 2, 0, 0]
        },
        barWidth: '40%'
      },
      {
        name: '最高连板',
        type: 'line',
        data: max_heights,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: {
          color: '#f59e0b',
          width: 2
        },
        itemStyle: {
          color: '#f59e0b'
        },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(245, 158, 11, 0.2)' },
            { offset: 1, color: 'transparent' }
          ])
        }
      },
      {
        name: '炸板率',
        type: 'line',
        yAxisIndex: 1,
        data: broken_ratios,
        smooth: true,
        symbol: 'diamond',
        symbolSize: 5,
        lineStyle: {
          color: '#6366f1',
          width: 2,
          type: 'dashed'
        },
        itemStyle: {
          color: '#6366f1'
        }
      }
    ]
  };
  
  chartInstance.setOption(option, true);
};

// 监听数据变化
watch(
  () => props.data,
  () => {
    nextTick(() => updateChart());
  },
  { deep: true }
);

// 监听加载状态
watch(
  () => props.loading,
  (loading) => {
    if (!loading) {
      nextTick(() => updateChart());
    }
  }
);

onMounted(() => {
  initChart();
  if (props.data) {
    updateChart();
  }
});

onUnmounted(() => {
  chartInstance?.dispose();
  window.removeEventListener('resize', () => chartInstance?.resize());
});
</script>

<style scoped>
.limit-up-trend-chart {
  position: relative;
  background: transparent;
}
</style>
