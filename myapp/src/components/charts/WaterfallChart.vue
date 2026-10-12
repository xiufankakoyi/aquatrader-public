<template>
  <div class="waterfall-chart-container">
    <div ref="chartContainer" class="w-full h-full"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

interface WaterfallData {
  name: string;
  value: number;
  itemStyle?: { color?: string };
}

interface Props {
  data: WaterfallData[];
  title?: string;
  subtitle?: string;
  unit?: string;
}

const props = withDefaults(defineProps<Props>(), {
  data: () => [],
  title: '',
  subtitle: '',
  unit: '元'
});

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 初始化图表
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

// 更新图表
function updateChart() {
  if (!chartInstance) return;

  // 计算累计值
  let sum = 0;
  const cumulativeData = props.data.map(item => {
    const currentSum = sum;
    sum += item.value;
    return {
      name: item.name,
      value: item.value,
      from: currentSum,
      to: sum
    };
  });

  // 设置颜色
  const data = props.data.map((item, index) => {
    let color = '#6366f1'; // 默认蓝色
    if (item.itemStyle?.color) {
      color = item.itemStyle.color;
    } else if (item.value >= 0) {
      color = '#10b981'; // 盈利绿色
    } else {
      color = '#ef4444'; // 亏损红色
    }
    return {
      ...item,
      itemStyle: { color }
    };
  });

  const option: EChartsOption = {
    title: {
      text: props.title,
      subtext: props.subtitle,
      textStyle: {
        color: 'var(--text-primary)'
      },
      subtextStyle: {
        color: 'var(--text-secondary)'
      }
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow'
      },
      formatter: (params: any) => {
        if (!Array.isArray(params) || params.length === 0) return '';
        
        const param = params[0];
        const value = param.value;
        const formattedValue = value.toLocaleString('zh-CN', {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2
        });
        const sign = value >= 0 ? '+' : '';
        
        return `
          <div style="padding: 8px; background: rgba(0, 0, 0, 0.85); border-radius: 6px; border: 1px solid ${param.color};">
            <div style="font-weight: bold; font-size: 14px; color: var(--text-primary); margin-bottom: 4px;">${param.name}</div>
            <div style="font-family: monospace; font-size: 16px; font-weight: bold; color: ${param.color};">
              ${sign}${formattedValue} ${props.unit}
            </div>
          </div>
        `;
      }
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: props.data.map(item => item.name),
      axisLine: {
        lineStyle: {
          color: 'var(--border-color)'
        }
      },
      axisLabel: {
        color: 'var(--text-secondary)',
        rotate: 45,
        fontSize: 12
      }
    },
    yAxis: {
      type: 'value',
      name: props.unit,
      axisLine: {
        lineStyle: {
          color: 'var(--border-color)'
        }
      },
      axisLabel: {
        color: 'var(--text-secondary)',
        formatter: (value: number) => {
          return value.toLocaleString('zh-CN');
        }
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: 'var(--border-color)',
          type: 'dashed'
        }
      }
    },
    series: [
      {
        name: '瀑布图',
        type: 'bar',
        data: data,
        label: {
          show: true,
          position: 'top',
          formatter: (params: any) => {
            const value = params.value;
            const sign = value >= 0 ? '+' : '';
            return `${sign}${value.toFixed(2)}`;
          },
          color: 'var(--text-primary)',
          fontSize: 11
        },
        emphasis: {
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(0, 0, 0, 0.5)'
          }
        },
        barWidth: '60%'
      },
      {
        name: '累计值',
        type: 'line',
        data: cumulativeData.map(item => item.to),
        smooth: true,
        lineStyle: {
          width: 2,
          color: '#8b5cf6'
        },
        itemStyle: {
          color: '#8b5cf6'
        },
        symbol: 'circle',
        symbolSize: 6,
        emphasis: {
          symbolSize: 8
        }
      }
    ]
  };

  chartInstance.setOption(option);
}

// 监听数据变化
watch(() => props.data, () => {
  updateChart();
}, { deep: true });

watch(() => [props.title, props.subtitle, props.unit], () => {
  updateChart();
});

// 窗口大小变化时调整图表
function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
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
</script>

<style scoped>
.waterfall-chart-container {
  position: relative;
  width: 100%;
  height: 300px;
}
</style>