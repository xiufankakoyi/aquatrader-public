<!--
  雷达图组件
  显示五维评分：超额收益、风险一致性、最大回撤适配度、交易结构健壮性、抗拟合可信度
-->
<template>
  <div class="radar-chart-container">
    <div ref="chartContainer" class="w-full h-80"></div>
    
    <!-- 评分说明 -->
    <div class="mt-4 grid grid-cols-2 md:grid-cols-5 gap-2 text-xs text-gray-600 dark:text-slate-400">
      <div>
        <p class="font-semibold">超额收益</p>
        <p>策略期间收益 vs 基准期间收益</p>
      </div>
      <div>
        <p class="font-semibold">风险一致性</p>
        <p>波动率稳定性、前后期夏普一致性</p>
      </div>
      <div>
        <p class="font-semibold">回撤适配度</p>
        <p>最大回撤控制、卡玛比率</p>
      </div>
      <div>
        <p class="font-semibold">交易健壮性</p>
        <p>胜率、盈亏比、平均盈亏比</p>
      </div>
      <div>
        <p class="font-semibold">抗拟合可信度</p>
        <p>月度收益稳定性、前后期表现一致性、索提诺比率</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

interface RadarScores {
  excessReturn: number;
  riskConsistency: number;
  maxDrawdown: number;
  tradingQuality: number;
  antiOverfitting: number;
}

interface Props {
  scores: RadarScores;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 初始化图表
function initChart() {
  if (!chartContainer.value) return;

  chartInstance = markRaw(echarts.init(chartContainer.value));
  updateChart();
}

// 更新图表
function updateChart() {
  if (!chartInstance) return;

  const option: EChartsOption = {
    radar: {
      indicator: [
        { name: '超额收益', max: 100 },
        { name: '风险一致性', max: 100 },
        { name: '回撤适配度', max: 100 },
        { name: '交易健壮性', max: 100 },
        { name: '抗拟合可信度', max: 100 }
      ],
      center: ['50%', '50%'],
      radius: '70%',
      axisName: {
        fontSize: 12,
        color: '#666'
      },
      splitArea: {
        show: true,
        areaStyle: {
          color: ['rgba(250, 250, 250, 0.3)', 'rgba(200, 200, 200, 0.3)']
        }
      },
      splitLine: {
        lineStyle: {
          color: '#ddd'
        }
      }
    },
    series: [{
      type: 'radar',
      data: [{
        value: [
          props.scores.excessReturn,
          props.scores.riskConsistency,
          props.scores.maxDrawdown,
          props.scores.tradingQuality,
          props.scores.antiOverfitting
        ],
        name: '综合评分',
        areaStyle: {
          color: 'rgba(37, 99, 235, 0.15)'
        },
        lineStyle: {
          color: '#2563eb',
          width: 2
        },
        itemStyle: {
          color: '#2563eb'
        }
      }]
    }],
    tooltip: {
      trigger: 'item',
      formatter: (params: any) => {
        const names = ['超额收益', '风险一致性', '回撤适配度', '交易健壮性', '抗拟合可信度'];
        let html = '<div class="text-sm">';
        params.value.forEach((val: number, index: number) => {
          html += `<p>${names[index]}: ${val.toFixed(1)} 分</p>`;
        });
        html += '</div>';
        return html;
      }
    }
  };

  chartInstance.setOption(option);
}

// 监听数据变化
watch(() => props.scores, () => {
  updateChart();
}, { deep: true });

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
.radar-chart-container {
  width: 100%;
}
</style>

