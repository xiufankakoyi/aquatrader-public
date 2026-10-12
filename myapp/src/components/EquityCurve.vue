<template>
  <div class="equity-curve-container relative w-full h-full">
    <div ref="chartContainer" class="w-full h-full"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw, computed } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';
import { useBacktestStore } from '../store/backtestStore';

/**
 * EquityCurve 组件
 * 仅用于展示权益曲线（使用 ECharts）
 * K线图功能已迁移到 TVKlineChart 组件
 *
 * 【流式更新优化】
 * - 使用增量更新而非完全重绘
 * - 添加防抖机制减少重绘频率
 * - 流式模式下暂停 localStorage 持久化
 *
 * 【交互优化】
 * - 十字线与曲线交点显示实心圆标记
 * - 净值曲线最高点/最低点灰色小字标注
 */

const backtestStore = useBacktestStore();

// 防抖定时器
let updateDebounceTimer: ReturnType<typeof setTimeout> | null = null;
let lastDataLength = 0;

interface EquityCurveVersion {
  versionId: string;
  versionName: string;
  data: Array<{ date: string; equity: number }>;
}

interface Props {
  versions?: EquityCurveVersion[];
  benchmark?: Array<{ date: string; equity: number }>;
  highlightRanges?: Array<{ start: string; end: string }>;
  scale?: 'linear' | 'log';
  shadowSeries?: Array<{ date: string; equity: number }>; // Logic Sandbox
  syncXAxis?: string; // 为十字线联动提供支持
}

const props = withDefaults(defineProps<Props>(), {
  versions: () => [],
  benchmark: () => [],
  highlightRanges: () => [],
  scale: 'linear',
  shadowSeries: () => [],
  syncXAxis: ''
});

const emit = defineEmits<{
  hover: [data: {
    date: string;
    version?: string;
    equity: number;
    monthlyReturn?: number;
  }];
}>();

const chartContainer = ref<HTMLDivElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

// 默认颜色
const versionColors = [
  '#2962ff', // 蓝色
  '#9c27b0', // 紫色
  '#089981', // 绿色
  '#f23645', // 红色
  '#ff9800', // 橙色
];

/**
 * 格式化数值为紧凑显示（万/亿）
 */
function formatCompact(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e8) return (value / 1e8).toFixed(2) + '亿';
  if (abs >= 1e4) return (value / 1e4).toFixed(2) + '万';
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * 初始化图表
 */
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

  // 监听鼠标移动
  chartInstance.on('mousemove', (params: any) => {
    const data = params.data;
    if (params.componentType === 'series' && data) {
      const equity = typeof data[2] === 'number' ? data[2] : parseFloat(data[1] || data[2] || '0') || 0;
      emit('hover', {
        date: data[0] || '',
        version: data[1] || params.seriesName || '',
        equity: equity,
      });
    }
  });

  // 监听 syncXAxis
  watch(() => props.syncXAxis, (newVal) => {
    if (chartInstance && newVal) {
      chartInstance.dispatchAction({
        type: 'showTip',
        seriesIndex: 0,
        dataIndex: props.versions[0]?.data.findIndex(d => d.date === newVal)
      });
    }
  });

  // 处理图表点击
  chartInstance.on('click', (params: any) => {
    let date = '';
    if (params.axisValue) {
      date = typeof params.axisValue === 'string' ? params.axisValue :
             new Date(params.axisValue).toISOString().split('T')[0];
    } else if (params.data && Array.isArray(params.data)) {
      date = params.data[0];
    }

    if (date && date.match(/^\d{4}-\d{2}-\d{2}/)) {
      backtestStore.setPlaybackCursor(date);
      backtestStore.togglePlaybackMode(true);
    }
  });

  chartInstance.on('mouseout', () => {
    emit('hover', { date: '', equity: 0 });
  });

  updateChart();
}

/**
 * 更新图表
 */
function updateChart() {
  if (!chartInstance) {
    initChart();
    return;
  }

  const series: any[] = [];

  // 获取所有日期
  const allDates = new Set<string>();
  props.versions.forEach(v => v.data.forEach(d => allDates.add(d.date)));
  props.benchmark.forEach(d => allDates.add(d.date));
  const sortedDates = Array.from(allDates).sort();

  // Equity Curve versions
  props.versions.forEach((version, index) => {
    const equityMap = new Map(version.data.map(d => [d.date, d.equity]));
    const data = sortedDates.map(date => {
      const equity = equityMap.get(date);
      if (equity === undefined) return null;
      return {
        name: date,
        value: [date, equity]
      };
    }).filter(d => d !== null) as any[];

    const color = index === 0 ? '#2962ff' : versionColors[index % versionColors.length];

    series.push({
      id: index === 0 ? 'strategy-equity' : `equity-${index}`,
      name: version.versionName,
      type: 'line',
      data: data,
      smooth: true,
      lineStyle: {
        width: 2,
        color: color
      },
      itemStyle: {
        color: color
      },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: color + '40' },  // 25% 透明度
          { offset: 0.5, color: color + '10' }, // 6% 透明度
          { offset: 1, color: color + '00' }    // 0% 透明度
        ])
      },
      symbol: 'none',
      emphasis: {
        focus: 'series',
        lineStyle: { width: 3 },
        itemStyle: {
          color: color,
          borderColor: '#fff',
          borderWidth: 2
        }
      }
    });
  });

  // 添加基准数据
  if (props.benchmark.length > 0) {
    const benchmarkMap = new Map(props.benchmark.map(d => [d.date, d.equity]));
    const benchmarkData = sortedDates.map(date => {
      const equity = benchmarkMap.get(date);
      if (equity === undefined) return null;
      return {
        name: date,
        value: [date, equity]
      };
    }).filter(d => d !== null) as any[];

    series.push({
      name: '基准',
      type: 'line',
      data: benchmarkData,
      smooth: true,
      lineStyle: {
        width: 1.5,
        type: 'dashed',
        color: '#94a3b8'
      },
      itemStyle: {
        color: '#94a3b8'
      },
      symbol: 'none',
      emphasis: {
        focus: 'series',
        itemStyle: {
          color: '#94a3b8',
          borderColor: '#fff',
          borderWidth: 2
        }
      }
    });
  }

  // Shadow Curve (Logic Sandbox)
  if (props.shadowSeries && props.shadowSeries.length > 0) {
    const shadowMap = new Map(props.shadowSeries.map(d => [d.date, d.equity]));
    const shadowData = sortedDates.map(date => {
      const equity = shadowMap.get(date);
      if (equity === undefined) return null;
      return {
        name: date,
        value: [date, equity]
      };
    }).filter(d => d !== null) as any[];

    series.push({
      name: '模拟净值 (剔除成交)',
      type: 'line',
      data: shadowData,
      smooth: true,
      lineStyle: {
        width: 1.5,
        type: 'dashed',
        color: '#cbd5e1',
        opacity: 0.6
      },
      itemStyle: {
        color: '#cbd5e1'
      },
      symbol: 'none'
    });
  }

  // 计算净值曲线的最高点/最低点，用于 markPoint 标注
  const primaryData = props.versions[0]?.data || [];
  const markPointData: any[] = [];
  if (primaryData.length > 0) {
    let maxItem = primaryData[0];
    let minItem = primaryData[0];
    for (const item of primaryData) {
      if (item.equity > maxItem.equity) maxItem = item;
      if (item.equity < minItem.equity) minItem = item;
    }
    markPointData.push(
      {
        name: '最高',
        coord: [maxItem.date, maxItem.equity],
        value: maxItem.equity,
        itemStyle: { color: '#34d399' },
        symbol: 'circle',
        symbolSize: 6,
        label: {
          show: true,
          formatter: (p: any) => formatCompact(p.value),
          position: 'top',
          color: '#64748b',
          fontSize: 11,
          fontFamily: 'JetBrains Mono, monospace',
          distance: 8
        }
      },
      {
        name: '最低',
        coord: [minItem.date, minItem.equity],
        value: minItem.equity,
        itemStyle: { color: '#f87171' },
        symbol: 'circle',
        symbolSize: 6,
        label: {
          show: true,
          formatter: (p: any) => formatCompact(p.value),
          position: 'bottom',
          color: '#64748b',
          fontSize: 11,
          fontFamily: 'JetBrains Mono, monospace',
          distance: 8
        }
      }
    );
  }

  // 给第一个 series（净值曲线）添加 markPoint
  if (series.length > 0 && markPointData.length > 0) {
    series[0].markPoint = {
      data: markPointData,
      animation: true,
      silent: true
    };
  }

  const option: any = {
    axisPointer: {
      link: { xAxisIndex: 'all' },
      label: {
        backgroundColor: 'rgba(30, 41, 59, 0.9)',
        borderColor: '#475569',
        borderWidth: 1,
        borderRadius: 4,
        padding: [4, 8],
        color: '#e2e8f0',
        fontSize: 11
      },
      lineStyle: {
        color: '#64748b',
        width: 1,
        type: 'dashed'
      },
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15, 23, 42, 0.85)',
      borderColor: 'rgba(71, 85, 105, 0.5)',
      borderWidth: 1,
      borderRadius: 8,
      padding: [12, 16],
      textStyle: {
        color: '#e2e8f0',
        fontSize: 12,
        fontFamily: 'JetBrains Mono, monospace'
      },
      extraCssText: 'backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);',
      axisPointer: {
        type: 'cross',
        crossStyle: {
          color: '#64748b',
          width: 1,
          type: 'dashed'
        },
        lineStyle: {
          color: '#64748b',
          width: 1,
          type: 'dashed'
        },
        label: {
          show: false
        }
      },
      formatter: (params: any) => {
        if (!Array.isArray(params) || params.length === 0) return '';
        let dateStr = params[0].axisValue;
        if (typeof dateStr === 'number') dateStr = new Date(dateStr).toISOString().split('T')[0];

        // 格式化日期显示
        const date = new Date(dateStr);
        const formattedDate = `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;

        let html = `<div style="margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid rgba(71, 85, 105, 0.5);">`;
        html += `<div style="font-weight: 600; font-size: 13px; color: #f8fafc;">${formattedDate}</div>`;
        html += `</div>`;

        params.forEach((item: any) => {
          const rawValue = Array.isArray(item.value) ? item.value[1] : item.value;
          const formattedValue = (typeof rawValue === 'number') ? rawValue.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
          }) : rawValue;

          // 计算收益率
          let returnRate = '';
          if (typeof rawValue === 'number' && rawValue > 0) {
            const rate = ((rawValue - 1000000) / 1000000 * 100).toFixed(2);
            const isPositive = parseFloat(rate) >= 0;
            returnRate = `<span style="color: ${isPositive ? '#34d399' : '#f87171'}; font-size: 11px; margin-left: 8px;">${isPositive ? '+' : ''}${rate}%</span>`;
          }

          html += `
            <div style="display:flex; justify-content:space-between; align-items:center; min-width:200px; margin: 6px 0; padding: 4px 0;">
              <div style="display:flex; align-items:center; gap:8px;">
                <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:${item.color};"></span>
                <span style="color:#94a3b8; font-size:12px;">${item.seriesName}</span>
              </div>
              <div style="display:flex; align-items:center;">
                <span style="font-family:JetBrains Mono, monospace; font-weight:600; color: #f1f5f9; font-size:13px;">${formattedValue}</span>
                ${returnRate}
              </div>
            </div>
          `;
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
      top: '8%',
      bottom: '12%',
      containLabel: true,
      backgroundColor: 'transparent'
    },
    xAxis: {
      type: 'time',
      boundaryGap: [0, 0],
      axisLine: {
        lineStyle: {
          color: 'rgba(71, 85, 105, 0.5)'
        }
      },
      axisLabel: {
        color: '#64748b',
        fontSize: 11,
        formatter: (value: number) => {
          const date = new Date(value);
          const year = date.getFullYear();
          const month = (date.getMonth() + 1).toString().padStart(2, '0');
          const day = date.getDate().toString().padStart(2, '0');

          // 获取数据范围
          const startDate = sortedDates.length > 0 ? new Date(sortedDates[0]) : null;
          const endDate = sortedDates.length > 0 ? new Date(sortedDates[sortedDates.length - 1]) : null;

          if (!startDate || !endDate) return `${month}/${day}`;

          // 如果跨年，显示年份
          const spansYear = startDate.getFullYear() !== endDate.getFullYear();

          if (spansYear) {
            // 1月或跨年时显示年份
            if (month === '01' || date.getTime() - startDate.getTime() < 30 * 24 * 60 * 60 * 1000) {
              return `${year}/${month}`;
            }
            return `${month}/${day}`;
          } else {
            // 同一年，不显示年份
            return `${month}/${day}`;
          }
        },
        hideOverlap: true
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: 'rgba(71, 85, 105, 0.2)',
          type: 'dashed'
        }
      }
    },
    yAxis: [
      {
        type: props.scale === 'log' ? 'log' : 'value',
        scale: true,
        position: 'right',
        logBase: 10,
        axisLine: { show: false },
        axisLabel: {
          color: '#64748b',
          fontSize: 10,
          formatter: (value: number) => {
            if (value >= 1000000) {
              return (value / 1000000).toFixed(2) + 'M';
            }
            return value.toLocaleString();
          }
        },
        splitLine: {
          show: true,
          lineStyle: {
            color: 'rgba(71, 85, 105, 0.2)',
            type: 'dashed'
          }
        }
      },
      {
        type: 'value',
        show: false,
        scale: true,
        position: 'left'
      }
    ],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: 0,
        filterMode: 'filter',
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: true,
        preventDefaultMouseMove: true
      },
      {
        type: 'slider',
        xAxisIndex: 0,
        height: 6,
        bottom: '2%',
        handleSize: '100%',
        borderColor: 'transparent',
        backgroundColor: 'rgba(30,41,59,0.3)',
        fillerColor: 'rgba(100,116,139,0.3)',
        handleStyle: {
          color: '#64748b',
          borderColor: '#475569',
          borderWidth: 1
        },
        showDetail: false
      }
    ],
    series: series
  };

  chartInstance.setOption(option, { notMerge: true, lazyUpdate: false });
}

/**
 * 增量更新图表数据
 * 流式模式下只追加新数据点，不重绘整个图表
 */
function incrementalUpdateChart() {
  if (!chartInstance) {
    console.warn('[EquityCurve] chartInstance 不存在');
    return;
  }

  const currentDataLength = props.versions[0]?.data.length || 0;
  const isStreaming = backtestStore.running;

  console.log('[EquityCurve] incrementalUpdateChart:', currentDataLength, 'points, lastDataLength:', lastDataLength, 'isStreaming:', isStreaming);

  // 如果是流式模式且数据量增加，使用增量更新
  if (isStreaming && currentDataLength > lastDataLength && lastDataLength > 0) {
    const newDataPoints = props.versions[0].data.slice(lastDataLength);
    if (newDataPoints.length > 0) {
      console.log('[EquityCurve] 增量更新:', newDataPoints.length, '新数据点');
      // 使用 setOption 的 merge 模式追加数据
      const newData = newDataPoints.map(d => ({
        name: d.date,
        value: [d.date, d.equity]
      }));

      chartInstance.setOption({
        series: [{
          id: 'strategy-equity',
          data: props.versions[0].data.map(d => ({
            name: d.date,
            value: [d.date, d.equity]
          }))
        }]
      }, { notMerge: false });

      lastDataLength = currentDataLength;
      return;
    }
  }

  // 非流式模式或数据量减少，完整更新
  console.log('[EquityCurve] 完整更新图表');
  lastDataLength = currentDataLength;
  updateChart();
}

/**
 * 防抖更新
 * 流式模式下减少重绘频率
 */
function debouncedUpdate() {
  const isStreaming = backtestStore.running;
  const delay = isStreaming ? 100 : 0; // 流式模式下 100ms 防抖

  if (updateDebounceTimer) {
    clearTimeout(updateDebounceTimer);
  }

  updateDebounceTimer = setTimeout(() => {
    incrementalUpdateChart();
    updateDebounceTimer = null;
  }, delay);
}

// 监听数据变化 - 使用防抖更新
watch(() => props.versions, (newVal) => {
  console.log('[EquityCurve] versions 变化:', newVal?.[0]?.data?.length, 'points, running:', backtestStore.running);
  debouncedUpdate();
}, { deep: true });

watch(() => props.benchmark, () => {
  debouncedUpdate();
}, { deep: true });

watch(() => props.shadowSeries, () => {
  debouncedUpdate();
}, { deep: true });

watch(() => props.scale, () => {
  updateChart();
});

// Sync Playback Mark
watch(() => backtestStore.playbackCursor, (newDate) => {
  if (!chartInstance || !newDate) return;
  chartInstance.dispatchAction({
    type: 'showTip',
    seriesIndex: 0,
    dataIndex: props.versions[0]?.data.findIndex(d => d.date === newDate) || 0
  });
});

// 窗口大小变化时调整图表
function handleResize() {
  chartInstance?.resize();
}

onMounted(() => {
  nextTick(() => {
    initChart();
    if (chartInstance) {
      chartInstance.resize();
      // 【修复】如果挂载时已有数据，立即渲染
      if (props.versions.length > 0 && props.versions[0].data.length > 0) {
        console.log('[EquityCurve] 挂载时已有数据，立即渲染:', props.versions[0].data.length, 'points');
        updateChart();
      }
    }
    window.addEventListener('resize', handleResize);
  });
});

onUnmounted(() => {
  // 清理防抖定时器
  if (updateDebounceTimer) {
    clearTimeout(updateDebounceTimer);
    updateDebounceTimer = null;
  }
  window.removeEventListener('resize', handleResize);
  chartInstance?.dispose();
  chartInstance = null;
});
</script>

<style scoped>
.equity-curve-container {
  position: relative;
  width: 100%;
  height: 100%;
}
</style>
