<template>
  <div class="position-pie-chart-container">
    <div ref="chartContainer" class="w-full h-full"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

interface PositionData {
  name: string;
  value: number;
  itemStyle?: { color?: string };
  symbol?: string;
}

interface Props {
  data: PositionData[];
  title?: string;
  subtitle?: string;
  unit?: string;
  showLegend?: boolean;
  showPercentage?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  data: () => [],
  title: '',
  subtitle: '',
  unit: '元',
  showLegend: true,
  showPercentage: true
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

  // 生成颜色数组
  const colors = [
    '#8b5cf6', '#6366f1', '#3b82f6', '#06b6d4',
    '#10b981', '#22c55e', '#84cc16', '#f59e0b',
    '#f97316', '#ef4444', '#ec4899', '#a855f7'
  ];

  // 处理数据，添加颜色和格式化
  const data = props.data.map((item, index) => {
    return {
      ...item,
      itemStyle: {
        color: item.itemStyle?.color || colors[index % colors.length]
      }
    };
  });

  const totalValue = data.reduce((sum, item) => sum + item.value, 0);

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
      trigger: 'item',
      formatter: (params: any) => {
        const name = params.name;
        const value = params.value;
        const percentage = ((value / totalValue) * 100).toFixed(2);
        const formattedValue = value.toLocaleString('zh-CN', {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2
        });
        
        return `
          <div style="padding: 8px; background: rgba(0, 0, 0, 0.85); border-radius: 6px; border: 1px solid ${params.color};">
            <div style="font-weight: bold; font-size: 14px; color: var(--text-primary); margin-bottom: 4px;">
              ${name} ${params.data.symbol ? `(${params.data.symbol})` : ''}
            </div>
            <div style="display: flex; justify-content: space-between; margin: 4px 0;">
              <span style="color: var(--text-secondary);">持仓金额:</span>
              <span style="font-family: monospace; font-weight: bold; color: ${params.color};">
                ${formattedValue} ${props.unit}
              </span>
            </div>
            <div style="display: flex; justify-content: space-between; margin: 4px 0;">
              <span style="color: var(--text-secondary);">占比:</span>
              <span style="font-family: monospace; font-weight: bold; color: ${params.color};">
                ${percentage}%
              </span>
            </div>
          </div>
        `;
      }
    },
    legend: props.showLegend ? {
      orient: 'vertical',
      right: '5%',
      top: 'center',
      textStyle: {
        color: 'var(--text-primary)'
      },
      formatter: (name: string) => {
        const item = data.find(item => item.name === name);
        if (item) {
          const percentage = ((item.value / totalValue) * 100).toFixed(1);
          return `${name} (${percentage}%)`;
        }
        return name;
      }
    } : undefined,
    grid: {
      left: '3%',
      right: props.showLegend ? '25%' : '3%',
      bottom: '3%',
      containLabel: true
    },
    series: [
      {
        name: '持仓分布',
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 8,
          borderColor: 'var(--bg-card)',
          borderWidth: 2
        },
        label: {
          show: true,
          position: 'outside',
          formatter: (params: any) => {
            if (props.showPercentage) {
              return `${params.name}: ${params.percent.toFixed(1)}%`;
            }
            return params.name;
          },
          color: 'var(--text-primary)',
          fontSize: 12
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 14,
            fontWeight: 'bold'
          },
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(0, 0, 0, 0.5)'
          }
        },
        labelLine: {
          show: true,
          lineStyle: {
            color: 'var(--border-color)'
          }
        },
        data: data
      }
    ]
  };

  chartInstance.setOption(option);
}

// 监听数据变化
watch(() => props.data, () => {
  updateChart();
}, { deep: true });

watch(() => [props.title, props.subtitle, props.unit, props.showLegend, props.showPercentage], () => {
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
.position-pie-chart-container {
  position: relative;
  width: 100%;
  height: 300px;
}
</style>