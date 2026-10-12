<!--
  RevenueProfitCashflowChart - P1.9 章节五「产品驱动和盈利预测」配套图表

  设计原则:
    1. 数据驱动: 仅渲染有数据系列，缺数据系列自动跳过
    2. 降级透明: 全空时显示「暂无本地证据」
    3. 历史 vs 前瞻: 实线为历史披露，虚线为前瞻预测，零轴对齐
    4. 中文 DocString + 字段说明，不输出交易动作建议
    5. 数值格式: 金额用 万/亿，百分比 2 位，分数 4 位
-->
<template>
  <div class="revenue-profit-cashflow-chart">
    <header class="chart-header">
      <div class="chart-title-block">
        <h3 class="chart-title">营业收入 / 净利润 / 经营性现金流</h3>
        <p class="chart-subtitle">
          实线为历史披露，虚线为前瞻预测；零轴对齐；不输出交易建议
        </p>
      </div>
      <div class="chart-badges">
        <span v-if="hasHistorical" class="badge badge--history">历史</span>
        <span v-if="hasForward" class="badge badge--forward">前瞻</span>
        <span v-if="!hasAnyData" class="badge badge--empty">无证据</span>
      </div>
    </header>

    <div v-if="hasAnyData" ref="chartContainer" class="chart-container"></div>
    <div v-else class="chart-empty">
      <i class="fas fa-folder-open"></i>
      <span>暂无本地证据</span>
    </div>

    <p v-if="hasAnyData" class="chart-disclaimer">
      本图表仅展示结构化证据，不构成买入、卖出、仓位、止盈或止损建议。
    </p>
  </div>
</template>

<script setup lang="ts">
/**
 * 营业收入 / 净利润 / 经营性现金流 三系列柱状+折线复合图。
 *
 * 数据来源：CFA 报告章节 5（drivers_and_forecast）与章节 4（financial_quality）
 * 历史来自 financial_history，前瞻来自 integrated_forecast。
 */
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw, computed } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import { formatAmount } from '@/utils/format';

/** 单期数据点。 */
export interface PeriodPoint {
  /** 期间标签（如 "2024A" / "2025E"）。 */
  period: string;
  /** 营业收入（元）。 */
  revenue?: number | null;
  /** 归母净利润（元）。 */
  net_profit?: number | null;
  /** 经营性现金流净额（元）。 */
  operating_cashflow?: number | null;
  /** 是否为前瞻预测（true 用虚线）。 */
  is_forecast?: boolean;
}

interface Props {
  /** 历史与预测期数据。 */
  data: PeriodPoint[];
  /** 单位（默认"元"）。 */
  unit?: string;
  /** 高度（px）。 */
  height?: number;
}

const props = withDefaults(defineProps<Props>(), {
  unit: '元',
  height: 320,
});

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

/** 是否有任何数据。 */
const hasAnyData = computed(() => {
  return props.data.some(
    (p) =>
      p.revenue != null ||
      p.net_profit != null ||
      p.operating_cashflow != null,
  );
});

/** 是否含历史数据。 */
const hasHistorical = computed(() =>
  props.data.some((p) => !p.is_forecast && (p.revenue != null || p.net_profit != null)),
);

/** 是否含前瞻数据。 */
const hasForward = computed(() =>
  props.data.some((p) => p.is_forecast && (p.revenue != null || p.net_profit != null)),
);

/** ECharts 数值轴 label：自动 万/亿。 */
function formatAxisLabel(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(1)} 亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)} 万`;
  return `${value.toFixed(0)}`;
}

/** tooltip 格式化。 */
function formatTooltip(params: any): string {
  if (!Array.isArray(params) || params.length === 0) return '';
  const period = params[0].axisValueLabel;
  const lines: string[] = [`<b>${period}</b>`];
  for (const p of params) {
    const v = typeof p.value === 'number' ? p.value : null;
    const formatted = v == null ? '-' : formatAmount(v);
    lines.push(
      `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px;"></span>${p.seriesName}: ${formatted}`,
    );
  }
  return lines.join('<br/>');
}

/** 初始化图表。 */
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

/** 更新图表配置。 */
function updateChart() {
  if (!chartInstance || !hasAnyData.value) return;
  const periods = props.data.map((p) => p.period);
  const revenues = props.data.map((p) =>
    p.revenue == null ? null : Number(p.revenue),
  );
  const netProfits = props.data.map((p) =>
    p.net_profit == null ? null : Number(p.net_profit),
  );
  const cashflows = props.data.map((p) =>
    p.operating_cashflow == null ? null : Number(p.operating_cashflow),
  );

  // 标记前瞻期 x 轴标签
  const forecastIndexes = new Set(
    props.data.filter((p) => p.is_forecast).map((_, idx) => idx),
  );

  const option: EChartsOption = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: formatTooltip,
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 12 },
    },
    legend: {
      data: ['营业收入', '归母净利润', '经营性现金流'],
      textStyle: { color: '#d1d4dc', fontSize: 11 },
      top: 0,
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: periods,
      axisLine: { lineStyle: { color: '#2a2e39' } },
      axisLabel: {
        color: '#787b86',
        fontSize: 11,
        formatter: (value: string, idx: number) => {
          return forecastIndexes.has(idx) ? `${value}*` : value;
        },
      },
    },
    yAxis: {
      type: 'value',
      name: props.unit,
      nameTextStyle: { color: '#787b86', fontSize: 11 },
      axisLine: { lineStyle: { color: '#2a2e39' } },
      axisLabel: {
        color: '#787b86',
        fontSize: 11,
        formatter: formatAxisLabel,
      },
      splitLine: {
        show: true,
        lineStyle: { color: '#1f2430', type: 'dashed' },
      },
    },
    series: [
      {
        name: '营业收入',
        type: 'bar',
        data: revenues,
        itemStyle: { color: '#5470c6' },
        barWidth: '30%',
      },
      {
        name: '归母净利润',
        type: 'line',
        data: netProfits,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { width: 2, color: '#52c41a' },
        itemStyle: { color: '#52c41a' },
      },
      {
        name: '经营性现金流',
        type: 'line',
        data: cashflows,
        smooth: true,
        symbol: 'diamond',
        symbolSize: 6,
        lineStyle: { width: 2, color: '#faad14', type: 'dashed' },
        itemStyle: { color: '#faad14' },
      },
    ],
  };
  chartInstance.setOption(option, true);
}

/** 窗口大小变化时调整图表。 */
function handleResize() {
  chartInstance?.resize();
}

watch(
  () => [props.data, props.unit],
  () => updateChart(),
  { deep: true },
);

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
.revenue-profit-cashflow-chart {
  background: rgba(20, 30, 50, 0.4);
  border: 1px solid #2a2e39;
  border-radius: 6px;
  padding: 12px;
}
.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 8px;
  flex-wrap: wrap;
  gap: 8px;
}
.chart-title {
  font-size: 13px;
  color: #d1d4dc;
  font-weight: 600;
  margin: 0;
}
.chart-subtitle {
  font-size: 11px;
  color: #787b86;
  margin: 2px 0 0 0;
}
.chart-badges {
  display: flex;
  gap: 4px;
}
.badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 500;
}
.badge--history {
  background: rgba(82, 196, 26, 0.18);
  color: #52c41a;
}
.badge--forward {
  background: rgba(84, 112, 198, 0.18);
  color: #5470c6;
}
.badge--empty {
  background: rgba(120, 123, 134, 0.18);
  color: #787b86;
}
.chart-container {
  width: 100%;
  height: v-bind('`${height}px`');
}
.chart-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: v-bind('`${height}px`');
  color: #787b86;
  font-size: 13px;
}
.chart-empty i {
  font-size: 24px;
  color: #5470c6;
}
.chart-disclaimer {
  margin-top: 8px;
  font-size: 11px;
  color: #787b86;
  text-align: center;
}
</style>
