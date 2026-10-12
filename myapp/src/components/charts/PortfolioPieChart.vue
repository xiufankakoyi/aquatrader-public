<template>
  <div ref="chartRef" class="w-full h-full"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted } from 'vue';
import * as echarts from 'echarts';

interface Position {
  stock_code: string;
  stock_name: string;
  market_value?: number;
  weight?: number;
  profit_loss_pct?: number;
}

const props = defineProps<{
  positions: Position[];
}>();

const chartRef = ref<HTMLElement | null>(null);
let chart: echarts.ECharts | null = null;

const initChart = () => {
  if (!chartRef.value) return;
  
  chart = echarts.init(chartRef.value, 'dark');
  updateChart();
};

const updateChart = () => {
  if (!chart) return;
  
  const data = props.positions
    .filter(p => p.market_value && p.market_value > 0)
    .map(p => ({
      name: p.stock_name,
      value: p.market_value,
      itemStyle: {
        color: getColorByProfit(p.profit_loss_pct || 0)
      }
    }));
  
  const option: echarts.EChartsOption = {
    tooltip: {
      trigger: 'item',
      formatter: (params: any) => {
        return `${params.name}<br/>市值: ${formatMoney(params.value)}<br/>占比: ${params.percent.toFixed(1)}%`;
      }
    },
    series: [
      {
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: {
          borderRadius: 4,
          borderColor: '#1a1f2e',
          borderWidth: 2
        },
        label: {
          show: true,
          formatter: '{b}\n{d}%',
          fontSize: 11,
          color: '#a0aec0'
        },
        labelLine: {
          show: true,
          length: 10,
          length2: 10
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 12,
            fontWeight: 'bold'
          },
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(0, 0, 0, 0.5)'
          }
        },
        data: data
      }
    ]
  };
  
  chart.setOption(option);
};

const getColorByProfit = (profitPct: number): string => {
  if (profitPct >= 10) return '#10b981';
  if (profitPct >= 5) return '#34d399';
  if (profitPct >= 0) return '#6ee7b7';
  if (profitPct >= -5) return '#f87171';
  if (profitPct >= -10) return '#ef4444';
  return '#dc2626';
};

const formatMoney = (value: number) => {
  if (value >= 10000) {
    return (value / 10000).toFixed(2) + '万';
  }
  return value.toFixed(2);
};

const handleResize = () => {
  chart?.resize();
};

watch(() => props.positions, updateChart, { deep: true });

onMounted(() => {
  initChart();
  window.addEventListener('resize', handleResize);
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
  chart?.dispose();
});
</script>
