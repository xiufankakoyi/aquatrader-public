<template>
  <div class="risk-metrics-chart-container">
    <div ref="chartContainer" class="w-full h-full"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick, markRaw } from 'vue';
import * as echarts from 'echarts';
import type { EChartsOption } from 'echarts';

interface VaRData {
  date: string;
  var: number;
  cvar: number;
  actualReturn: number;
}

interface Props {
  data: VaRData[];
  title?: string;
  subtitle?: string;
  confidenceLevel?: number;
  showActualReturns?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  data: () => [],
  title: '风险指标分析',
  subtitle: 'VaR 和 CVaR 历史波动',
  confidenceLevel: 95,
  showActualReturns: true
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

  const dates = props.data.map(item => item.date);
  const varData = props.data.map(item => item.var);
  const cvarData = props.data.map(item => item.cvar);
  const actualReturns = props.data.map(item => item.actualReturn);

  // 计算超出 VaR 的天数
  const varBreaches = props.data.filter(item => item.actualReturn < -item.var).length;
  const expectedBreaches = Math.round(props.data.length * ((100 - props.confidenceLevel) / 100));

  const option: EChartsOption = {
    title: {
      text: props.title,
      subtext: `${props.subtitle} - ${props.confidenceLevel}% 置信水平`,
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
        type: 'cross'
      },
      formatter: (params: any) => {
        if (!Array.isArray(params) || params.length === 0) return '';
        
        const date = params[0].axisValue;
        let html = `
          <div style="padding: 8px; background: rgba(0, 0, 0, 0.85); border-radius: 6px;">
            <div style="font-weight: bold; font-size: 14px; color: var(--text-primary); margin-bottom: 8px;">${date}</div>
        `;
        
        params.forEach((param: any) => {
          const value = param.value;
          const formattedValue = value.toLocaleString('zh-CN', {
            minimumFractionDigits: 3,
            maximumFractionDigits: 3
          });
          const sign = value >= 0 ? '+' : '';
          
          html += `
            <div style="display: flex; justify-content: space-between; margin: 4px 0;">
              <span style="margin-right: 10px; color: var(--text-secondary);">${param.marker} ${param.seriesName}:</span>
              <span style="font-family: monospace; font-weight: bold; color: ${param.color};">
                ${sign}${formattedValue}%
              </span>
            </div>
          `;
        });
        
        html += `</div>`;
        return html;
      }
    },
    legend: {
      data: ['VaR', 'CVaR', ...(props.showActualReturns ? ['实际收益'] : [])],
      top: 30,
      textStyle: {
        color: 'var(--text-primary)'
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
      data: dates,
      axisLine: {
        lineStyle: {
          color: 'var(--border-color)'
        }
      },
      axisLabel: {
        color: 'var(--text-secondary)',
        rotate: 45,
        fontSize: 12
      },
      splitLine: {
        show: false
      }
    },
    yAxis: {
      type: 'value',
      name: '收益率 (%)',
      axisLine: {
        lineStyle: {
          color: 'var(--border-color)'
        }
      },
      axisLabel: {
        color: 'var(--text-secondary)',
        formatter: (value: number) => {
          return value.toFixed(2) + '%';
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
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: 0,
        filterMode: 'filter',
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        preventDefaultMouseMove: true
      },
      {
        type: 'slider',
        xAxisIndex: 0,
        height: 6,
        bottom: '2%',
        handleSize: '100%',
        borderColor: 'transparent',
        backgroundColor: 'rgba(30,41,59,0.1)',
        fillerColor: 'rgba(100,116,139,0.2)',
        handleStyle: {
          color: '#64748b',
          borderColor: '#475569',
          borderWidth: 1
        },
        showDetail: false
      }
    ],
    series: [
      {
        name: 'VaR',
        type: 'line',
        data: varData,
        smooth: true,
        lineStyle: {
          width: 2,
          color: '#3b82f6',
          type: 'solid'
        },
        itemStyle: {
          color: '#3b82f6'
        },
        symbol: 'circle',
        symbolSize: 6,
        emphasis: {
          symbolSize: 8,
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(59, 130, 246, 0.5)'
          }
        },
        z: 3
      },
      {
        name: 'CVaR',
        type: 'line',
        data: cvarData,
        smooth: true,
        lineStyle: {
          width: 2,
          color: '#ef4444',
          type: 'solid'
        },
        itemStyle: {
          color: '#ef4444'
        },
        symbol: 'circle',
        symbolSize: 6,
        emphasis: {
          symbolSize: 8,
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(239, 68, 68, 0.5)'
          }
        },
        z: 2
      },
      ...(props.showActualReturns ? [{
        name: '实际收益',
        type: 'bar',
        data: actualReturns,
        itemStyle: {
          color: (params: any) => {
            const value = params.value;
            if (value >= 0) {
              return '#10b981'; // 正收益绿色
            } else if (value < -varData[params.dataIndex]) {
              return '#dc2626'; // 超出 VaR 的亏损深红色
            } else {
              return '#ef4444'; // 正常亏损红色
            }
          }
        },
        barWidth: '40%',
        emphasis: {
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(0, 0, 0, 0.5)'
          }
        },
        z: 1
      }] : [])
    ],
    graphic: {
      elements: [
        {
          type: 'text',
          left: 'right',
          top: 'bottom',
          style: {
            text: `VaR 突破次数: ${varBreaches}/${expectedBreaches} (预期)`,
            fontSize: 12,
            fill: 'var(--text-secondary)',
            textAlign: 'right'
          },
          z: 100
        }
      ]
    }
  };

  chartInstance.setOption(option);
}

// 监听数据变化
watch(() => props.data, () => {
  updateChart();
}, { deep: true });

watch(() => [props.title, props.subtitle, props.confidenceLevel, props.showActualReturns], () => {
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
.risk-metrics-chart-container {
  position: relative;
  width: 100%;
  height: 300px;
}
</style>