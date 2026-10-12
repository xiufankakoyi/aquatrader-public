<!--
  ProfitBridgeWaterfall - P1.9 章节五「产品驱动和盈利预测」配套利润桥瀑布图

  设计原则:
    1. 数据驱动: 仅渲染有数据，全空显示「暂无本地证据」
    2. 利润桥瀑布: 起点值 → 各驱动因素变化 → 终点值
    3. 增长用绿色，下降用红色，起点/终点用蓝色
    4. 不输出交易动作建议
    5. 数值格式: 金额用 万/亿
-->
<template>
  <div class="profit-bridge-waterfall">
    <header class="chart-header">
      <div class="chart-title-block">
        <h3 class="chart-title">利润桥瀑布图</h3>
        <p class="chart-subtitle">
          起点值 → 各驱动因素变化 → 终点值；增长绿色，下降红色
        </p>
      </div>
      <div class="chart-badges">
        <span v-if="hasData" class="badge badge--ok">{{ bridgeItems.length }} 项</span>
        <span v-else class="badge badge--empty">无证据</span>
      </div>
    </header>

    <div v-if="hasData" ref="chartContainer" class="chart-container"></div>
    <div v-else class="chart-empty">
      <i class="fas fa-folder-open"></i>
      <span>暂无本地证据</span>
    </div>

    <p v-if="hasData" class="chart-disclaimer">
      本图表仅展示结构化证据，不构成买入、卖出、仓位、止盈或止损建议。
    </p>
  </div>
</template>

<script setup lang="ts">
/**
 * 利润桥瀑布图。
 *
 * 数据来源：CFA 报告章节 5（drivers_and_forecast）的 historical_drivers +
 * forward_assumptions。每个驱动因素贡献正/负变化。
 */
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw, computed } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import { formatAmount } from '@/utils/format';

/** 单项驱动因素。 */
export interface BridgeItem {
  /** 项目名（如 "销量增长" / "ASP 提升" / "原材料成本下降"）。 */
  name: string;
  /** 变化值（元，正数表示贡献利润增加，负数表示减少）。 */
  delta: number;
  /** 项目类别（用于颜色区分）。 */
  kind?: 'start' | 'end' | 'driver' | 'adjustment';
}

interface Props {
  /** 利润桥各项。 */
  items: BridgeItem[];
  /** 起点值（如上期净利润）。 */
  startValue?: number | null;
  /** 终点值（如下期预测净利润）。 */
  endValue?: number | null;
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

/** 完整桥项列表（含起点/终点）。 */
const bridgeItems = computed<BridgeItem[]>(() => {
  const list: BridgeItem[] = [];
  if (props.startValue != null) {
    list.push({ name: '起点', delta: Number(props.startValue), kind: 'start' });
  }
  for (const item of props.items) {
    list.push({
      name: item.name,
      delta: Number(item.delta),
      kind: item.kind || 'driver',
    });
  }
  if (props.endValue != null) {
    list.push({ name: '终点', delta: Number(props.endValue), kind: 'end' });
  }
  return list;
});

const hasData = computed(() => bridgeItems.value.length > 0);

/** ECharts 数值轴 label：自动 万/亿。 */
function formatAxisLabel(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(1)} 亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)} 万`;
  return `${value.toFixed(0)}`;
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
  if (!chartInstance || !hasData.value) return;
  const items = bridgeItems.value;
  const names = items.map((i) => i.name);

  // 瀑布图：累计值 + 浮动 bar
  let cumulative = 0;
  const helperData: (number | null)[] = [];
  const positiveData: (number | null)[] = [];
  const negativeData: (number | null)[] = [];
  const totalData: (number | null)[] = [];

  for (const item of items) {
    const v = item.delta;
    if (item.kind === 'start' || item.kind === 'end') {
      // 起点/终点：固定 bar
      helperData.push(null);
      positiveData.push(null);
      negativeData.push(null);
      totalData.push(v);
    } else {
      // 驱动项：浮空 bar
      if (v >= 0) {
        helperData.push(cumulative);
        positiveData.push(v);
        negativeData.push(null);
      } else {
        helperData.push(cumulative + v);
        positiveData.push(null);
        negativeData.push(v);
      }
      cumulative += v;
      totalData.push(null);
    }
  }

  const option: EChartsOption = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 12 },
      formatter: (params: any) => {
        if (!Array.isArray(params) || params.length === 0) return '';
        const name = params[0].axisValueLabel;
        const lines: string[] = [`<b>${name}</b>`];
        for (const p of params) {
          if (p.value == null) continue;
          const formatted = formatAmount(p.value);
          lines.push(
            `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px;"></span>${p.seriesName}: ${formatted}`,
          );
        }
        return lines.join('<br/>');
      },
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: names,
      axisLine: { lineStyle: { color: '#2a2e39' } },
      axisLabel: {
        color: '#787b86',
        fontSize: 11,
        rotate: 30,
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
      // 辅助透明 bar（占位）
      {
        name: '辅助',
        type: 'bar',
        stack: 'bridge',
        itemStyle: { color: 'transparent' },
        emphasis: { itemStyle: { color: 'transparent' } },
        data: helperData,
        z: 1,
      },
      // 正向变化（绿色）
      {
        name: '正向贡献',
        type: 'bar',
        stack: 'bridge',
        data: positiveData,
        itemStyle: { color: '#52c41a' },
        label: {
          show: true,
          position: 'top',
          formatter: (p: any) =>
            p.value == null ? '' : `+${formatAmount(p.value)}`,
          color: '#52c41a',
          fontSize: 10,
        },
        z: 2,
      },
      // 负向变化（红色）
      {
        name: '负向贡献',
        type: 'bar',
        stack: 'bridge',
        data: negativeData,
        itemStyle: { color: '#ff4d4f' },
        label: {
          show: true,
          position: 'bottom',
          formatter: (p: any) =>
            p.value == null ? '' : formatAmount(p.value),
          color: '#ff4d4f',
          fontSize: 10,
        },
        z: 2,
      },
      // 起点/终点（蓝色实心）
      {
        name: '起点/终点',
        type: 'bar',
        data: totalData,
        itemStyle: { color: '#5470c6' },
        label: {
          show: true,
          position: 'top',
          formatter: (p: any) => (p.value == null ? '' : formatAmount(p.value)),
          color: '#5470c6',
          fontSize: 10,
        },
        z: 3,
      },
    ],
  };
  chartInstance.setOption(option, true);
}

function handleResize() {
  chartInstance?.resize();
}

watch(
  () => [props.items, props.startValue, props.endValue, props.unit],
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
.profit-bridge-waterfall {
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
.badge--ok {
  background: rgba(82, 196, 26, 0.18);
  color: #52c41a;
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
