<!--
  ValuationBandChart - P1.9 章节七「估值方法与敏感性」配套估值带图

  设计原则:
    1. 数据驱动: 仅渲染有数据，全空显示「暂无本地证据」
    2. 历史估值带: 阴影区域展示 PE/PB 历史区间 [low, high]
    3. Forward 估值: 单点标注各方法 output_per_share 中值
    4. 不输出交易动作建议
    5. 数值格式: 估值倍数 2 位
-->
<template>
  <div class="valuation-band-chart">
    <header class="chart-header">
      <div class="chart-title-block">
        <h3 class="chart-title">历史估值带与 Forward 估值</h3>
        <p class="chart-subtitle">
          阴影区域为历史 PE/PB 区间；散点为各估值方法 Forward 中值
        </p>
      </div>
      <div class="chart-badges">
        <span v-if="hasData" class="badge badge--ok">{{ methodCount }} 方法</span>
        <span v-else class="badge badge--empty">无证据</span>
      </div>
    </header>

    <div v-if="hasData" ref="chartContainer" class="chart-container"></div>
    <div v-else class="chart-empty">
      <i class="fas fa-folder-open"></i>
      <span>暂无本地证据</span>
    </div>

    <p v-if="hasData" class="chart-disclaimer">
      本图表输出区间与敏感性，不构成买入、卖出、仓位、止盈或止损建议。
    </p>
  </div>
</template>

<script setup lang="ts">
/**
 * 历史估值带 + Forward 估值散点图。
 *
 * 数据来源：CFA 报告章节 7（valuation_sensitivity）的
 * valuation_cases 与 sensitivity_points。
 */
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw, computed } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import { formatScore } from '@/utils/format';

/** 历史估值带数据点。 */
export interface BandPoint {
  /** 期间标签（如 "2024Q1"）。 */
  period: string;
  /** 区间下界。 */
  low: number;
  /** 区间上界。 */
  high: number;
  /** 中值（可选）。 */
  mid?: number;
}

/** Forward 估值散点。 */
export interface ForwardPoint {
  /** 方法名（如 "DCF(FCFF)" / "DDM" / "P/E"）。 */
  method: string;
  /** 每股价值中值。 */
  value: number;
  /** 区间下界（可选）。 */
  low?: number;
  /** 区间上界（可选）。 */
  high?: number;
}

interface Props {
  /** 历史估值带数据。 */
  bandData?: BandPoint[];
  /** Forward 估值散点。 */
  forwardPoints?: ForwardPoint[];
  /** Y 轴单位（默认"倍"）。 */
  unit?: string;
  /** 高度（px）。 */
  height?: number;
}

const props = withDefaults(defineProps<Props>(), {
  bandData: () => [],
  forwardPoints: () => [],
  unit: '倍',
  height: 320,
});

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const hasBand = computed(() => (props.bandData?.length || 0) > 0);
const hasForward = computed(() => (props.forwardPoints?.length || 0) > 0);
const hasData = computed(() => hasBand.value || hasForward.value);
const methodCount = computed(() => props.forwardPoints?.length || 0);

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
  const band = props.bandData || [];
  const forwards = props.forwardPoints || [];
  const periods = band.map((b) => b.period);
  const lows = band.map((b) => b.low);
  const highs = band.map((b) => b.high);
  const mids = band.map((b) => b.mid ?? (b.low + b.high) / 2);

  // Forward 散点：在最后一个 band period 之后排列
  const forwardX = forwards.map((_, idx) => `Forward${idx + 1}`);
  const allX = [...periods, ...forwardX];
  const forwardValues = forwards.map((f) => f.value);
  const forwardLows = forwards.map((f) => f.low ?? null);
  const forwardHighs = forwards.map((f) => f.high ?? null);

  const option: EChartsOption = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(19, 23, 34, 0.95)',
      borderColor: '#2a2e39',
      textStyle: { color: '#d1d4dc', fontSize: 12 },
      formatter: (params: any) => {
        if (!Array.isArray(params) || params.length === 0) return '';
        const name = params[0].axisValueLabel;
        const lines: string[] = [`<b>${name}</b>`];
        for (const p of params) {
          if (p.value == null) continue;
          const v = typeof p.value === 'number' ? p.value : null;
          if (v == null) continue;
          lines.push(
            `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px;"></span>${p.seriesName}: ${formatScore(v, 2)} ${props.unit}`,
          );
        }
        return lines.join('<br/>');
      },
    },
    legend: {
      data: ['历史下界', '历史上界', '历史中值', 'Forward 估值'],
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
      data: allX,
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
        formatter: (v: number) => v.toFixed(2),
      },
      splitLine: {
        show: true,
        lineStyle: { color: '#1f2430', type: 'dashed' },
      },
    },
    series: [
      // 历史下界（用于阴影区域）
      {
        name: '历史下界',
        type: 'line',
        data: [...lows, ...forwardLows],
        lineStyle: { opacity: 0 },
        stack: 'band',
        symbol: 'none',
        silent: true,
      },
      // 历史带宽（上界 - 下界，作为阴影区域）
      {
        name: '历史上界',
        type: 'line',
        data: [...highs.map((h, i) => h - lows[i]), ...forwardHighs.map((h, i) =>
          h == null || forwardLows[i] == null ? null : h - (forwardLows[i] as number),
        )],
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'rgba(84, 112, 198, 0.18)' },
        stack: 'band',
        symbol: 'none',
        z: 1,
      },
      // 历史中值线
      {
        name: '历史中值',
        type: 'line',
        data: [...mids, ...new Array(forwards.length).fill(null)],
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { width: 2, color: '#5470c6' },
        itemStyle: { color: '#5470c6' },
        z: 3,
      },
      // Forward 估值散点
      {
        name: 'Forward 估值',
        type: 'scatter',
        data: [
          ...new Array(periods.length).fill(null),
          ...forwardValues,
        ],
        symbolSize: 12,
        itemStyle: {
          color: '#faad14',
          borderColor: '#faad14',
          borderWidth: 2,
        },
        label: {
          show: true,
          position: 'top',
          formatter: (p: any) => {
            const idx = p.dataIndex - periods.length;
            if (idx < 0 || idx >= forwards.length) return '';
            return forwards[idx].method;
          },
          color: '#faad14',
          fontSize: 10,
        },
        z: 4,
      },
    ],
  };
  chartInstance.setOption(option, true);
}

function handleResize() {
  chartInstance?.resize();
}

watch(
  () => [props.bandData, props.forwardPoints, props.unit],
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
.valuation-band-chart {
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
