<!--
  热力图组件
  显示月度收益热力图
-->
<template>
  <div class="heatmap-chart-container">
    <div ref="chartContainer" class="w-full h-64"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, computed, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import type { MonthlyReturn } from '../types/backtest';

interface Props {
  data: MonthlyReturn[];
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 处理数据：转换为热力图格式
const heatmapData = computed(() => {
  const result: any[] = [];
  
  props.data.forEach((item) => {
    if (item.year && item.months) {
      item.months.forEach((value, monthIndex) => {
        if (value !== null && value !== undefined) {
          result.push({
            value: [monthIndex, item.year, value],
            itemStyle: {
              color: getHeatmapColor(value)
            }
          });
        }
      });
    }
  });
  
  return result;
});

// 获取热力图颜色
function getHeatmapColor(value: number): string {
  if (value >= 10) return '#065f46'; // 深绿
  if (value >= 5) return '#10b981'; // 绿
  if (value >= 2) return '#84cc16'; // 浅绿
  if (value > 0) return '#eab308'; // 黄
  if (value === 0) return '#94a3b8'; // 灰
  if (value > -2) return '#f97316'; // 橙
  if (value > -5) return '#ef4444'; // 红
  return '#991b1b'; // 深红
}

// 月份标签
const monthLabels = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

// 年份范围
const yearRange = computed(() => {
  const years = props.data
    .map(item => item.year)
    .filter((year): year is number => year !== undefined)
    .sort((a, b) => a - b);
  
  if (years.length === 0) return { min: 2024, max: 2024 };
  return { min: years[0], max: years[years.length - 1] };
});

// 初始化图表
function initChart() {
  if (!chartContainer.value) return;

  chartInstance = markRaw(echarts.init(chartContainer.value));
  updateChart();
}

// 更新图表
function updateChart() {
  if (!chartInstance) return;

  // 检查数据是否为空
  if (!props.data || props.data.length === 0) {
    chartInstance.setOption({
      title: {
        text: '暂无数据',
        left: 'center',
        top: 'middle',
        textStyle: {
          color: '#94a3b8',
          fontSize: 14
        }
      }
    });
    return;
  }

  // 检查是否有有效数据
  const hasValidData = props.data.some(item => item.year && item.months && item.months.some(v => v !== null && v !== undefined));
  if (!hasValidData) {
    chartInstance.setOption({
      title: {
        text: '暂无有效数据',
        left: 'center',
        top: 'middle',
        textStyle: {
          color: '#94a3b8',
          fontSize: 14
        }
      }
    });
    return;
  }

  const years: number[] = [];
  for (let y = yearRange.value.min; y <= yearRange.value.max; y++) {
    years.push(y);
  }

  const option: EChartsOption = {
    tooltip: {
      position: 'top',
      formatter: (params: any) => {
        const data = params.data.value;
        const date = `${data[1]}年${monthLabels[data[0]]}`;
        const returnValue = data[2].toFixed(2);
        return `${date}<br/>收益: ${returnValue >= 0 ? '+' : ''}${returnValue}%`;
      }
    },
    grid: {
      height: '65%',
      top: '8%',
      left: '8%',
      right: '8%',
      bottom: '15%'
    },
    xAxis: {
      type: 'category',
      data: monthLabels,
      splitArea: {
        show: true
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 11
      },
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      }
    },
    yAxis: {
      type: 'category',
      data: years.map(y => `${y}年`),
      splitArea: {
        show: true
      },
      axisLabel: {
        color: '#94a3b8',
        fontSize: 11
      },
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      },
      inverse: true // 年份倒序显示（最新的在上）
    },
    visualMap: {
      min: -10,
      max: 10,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: '5%',
      inRange: {
        color: ['#ef4444', '#94a3b8', '#10b981'] // 红 -> 灰 -> 绿
      },
      textStyle: {
        color: '#94a3b8'
      }
    },
    series: [{
      name: '月度收益',
      type: 'heatmap',
      data: heatmapData.value,
      label: {
        show: true,
        color: '#fff',
        fontSize: 11,
        fontWeight: 'bold',
        formatter: (params: any) => {
          const value = params.data.value[2];
          return value !== null && value !== undefined ? value.toFixed(1) + '%' : '';
        }
      },
      emphasis: {
        itemStyle: {
          shadowBlur: 10,
          shadowColor: 'rgba(0, 0, 0, 0.5)',
          borderWidth: 2,
          borderColor: '#fff'
        }
      }
    }]
  };

  chartInstance.setOption(option, { notMerge: true, lazyUpdate: false });
}

// 监听数据变化
watch(() => props.data, () => {
  if (chartInstance) {
    updateChart();
  }
}, { deep: true });

watch(() => heatmapData.value, () => {
  if (chartInstance) {
  updateChart();
  }
}, { deep: true });

// 窗口大小变化时调整图表
function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
  nextTick(() => {
    initChart();
    if (chartInstance) {
      chartInstance.resize();
      // 确保数据更新后重新渲染
      setTimeout(() => {
        updateChart();
      }, 100);
    }
    window.addEventListener('resize', handleResize);
  });
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});
</script>

<style scoped>
.heatmap-chart-container {
  width: 100%;
}
</style>

