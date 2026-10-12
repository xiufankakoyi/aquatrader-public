<template>
  <div class="defense-pie-container">
    <div ref="chartContainer" class="w-full h-64"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

interface DefenseData {
  name: string;
  value: number;
  color: string;
}

interface Props {
  data: DefenseData[];
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

function initChart() {
  if (!chartContainer.value) return;
  chartInstance = markRaw(echarts.init(chartContainer.value));
  updateChart();
}

function updateChart() {
  if (!chartInstance) return;

  const option: EChartsOption = {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c}% ({d}%)'
    },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      avoidLabelOverlap: false,
      itemStyle: {
        borderRadius: 8,
        borderColor: '#0b0e14',
        borderWidth: 2
      },
      label: {
        show: true,
        formatter: '{b}\n{c}%',
        color: '#e2e8f0',
        fontSize: 12
      },
      emphasis: {
        label: {
          show: true,
          fontSize: 14,
          fontWeight: 'bold'
        }
      },
      data: props.data.map(item => ({
        value: item.value,
        name: item.name,
        itemStyle: {
          color: item.color
        }
      }))
    }]
  };

  chartInstance.setOption(option);
}

watch(() => props.data, () => {
  updateChart();
}, { deep: true });

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
.defense-pie-container {
  width: 100%;
}
</style>

