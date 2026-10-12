<template>
  <div class="bubble-matrix-chart h-full flex flex-col">
    <!-- 图表容器 - 自适应父容器高度 -->
    <div ref="chartRef" class="flex-1 w-full min-h-0"></div>
    
    <!-- 加载状态 -->
    <div v-if="loading" class="absolute inset-0 flex items-center justify-center bg-slate-900/50 rounded-xl">
      <div class="flex items-center gap-2 text-slate-400">
        <i class="fas fa-spinner fa-spin"></i>
        <span>加载中...</span>
      </div>
    </div>
    
    <!-- 空状态 -->
    <div v-if="!loading && isEmpty" class="absolute inset-0 flex items-center justify-center">
      <div class="text-center text-slate-500">
        <i class="fas fa-circle-nodes text-4xl mb-2"></i>
        <p class="text-sm">暂无数据</p>
      </div>
    </div>
    
    <!-- 股票详情弹窗 -->
    <div
      v-if="selectedStock"
      class="absolute top-4 right-4 w-64 bg-slate-800/95 backdrop-blur border border-slate-700 rounded-lg p-4 shadow-xl z-10"
    >
      <div class="flex justify-between items-start mb-3">
        <div>
          <h4 class="text-white font-medium">{{ selectedStock.stock_name }}</h4>
          <p class="text-xs text-slate-400">{{ selectedStock.stock_code }}</p>
        </div>
        <button @click="selectedStock = null" class="text-slate-400 hover:text-white">
          <i class="fas fa-times"></i>
        </button>
      </div>
      
      <div class="space-y-2 text-xs">
        <div class="flex justify-between">
          <span class="text-slate-400">连板数</span>
          <span class="text-red-400 font-medium">{{ selectedStock.continue_num }} 板</span>
        </div>
        <div class="flex justify-between">
          <span class="text-slate-400">市值</span>
          <span class="text-slate-300">{{ selectedStock.market_cap }} 亿</span>
        </div>
        <div class="flex justify-between">
          <span class="text-slate-400">封板时间</span>
          <span class="text-slate-300">{{ selectedStock.limit_up_time_str }}</span>
        </div>
        <div class="flex justify-between">
          <span class="text-slate-400">封单额</span>
          <span class="text-slate-300">{{ (selectedStock.order_amount / 1e8).toFixed(2) }} 亿</span>
        </div>
        <div class="flex justify-between">
          <span class="text-slate-400">换手率</span>
          <span class="text-slate-300">{{ selectedStock.turnover_rate?.toFixed(2) }}%</span>
        </div>
        <div class="flex justify-between">
          <span class="text-slate-400">分类</span>
          <span :class="getQuadrantColor(selectedStock.quadrant)">{{ selectedStock.quadrant_name }}</span>
        </div>
        <div v-if="selectedStock.theme" class="pt-2 border-t border-slate-700">
          <span class="text-slate-400">题材：</span>
          <span class="text-slate-300">{{ selectedStock.theme }}</span>
        </div>
        <div v-if="selectedStock.tags?.length" class="flex flex-wrap gap-1 pt-2">
          <span
            v-for="tag in selectedStock.tags"
            :key="tag"
            class="px-1.5 py-0.5 bg-indigo-500/20 text-indigo-400 rounded text-[10px]"
          >
            {{ tag }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';

/**
 * BubbleMatrixChart - 涨停强度气泡图组件
 * 
 * 基于涨停强度实现四象限气泡图可视化：
 * - X轴：封板时间（早封板在左，晚封板在右）
 * - Y轴：市值（小市值在下，大市值在上）
 * - 气泡大小：封单额
 * - 四象限：权重股、跟风股、题材股、强势股
 */

interface BubbleData {
  stock_code: string;
  stock_name: string;
  continue_num: number;
  market_cap: number;
  limit_up_time: number;
  limit_up_time_str: string;
  order_amount: number;
  turnover_rate: number;
  quadrant: number;
  quadrant_name: string;
  theme: string;
  tags: string[];
}

interface MatrixData {
  date: string;
  bubbles: BubbleData[];
  quadrant_labels: Record<number, string>;
}

interface Props {
  data: MatrixData | null;
  loading?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  data: null,
  loading: false
});

const chartRef = ref<HTMLElement | null>(null);
let chartInstance: echarts.ECharts | null = null;

const isEmpty = ref(true);
const selectedStock = ref<BubbleData | null>(null);

/**
 * 获取象限颜色
 */
const getQuadrantColor = (quadrant: number): string => {
  const colors: Record<number, string> = {
    1: 'text-blue-400',   // 权重股
    2: 'text-amber-400',  // 跟风股
    3: 'text-pink-400',   // 题材股
    4: 'text-emerald-400' // 强势股
  };
  return colors[quadrant] || 'text-slate-400';
};

/**
 * 获取象限背景色
 */
const getQuadrantBgColor = (quadrant: number): string => {
  const colors: Record<number, string> = {
    1: 'rgba(59, 130, 246, 0.6)',   // 蓝色 - 权重股
    2: 'rgba(245, 158, 11, 0.6)',   // 琥珀色 - 跟风股
    3: 'rgba(236, 72, 153, 0.6)',   // 粉色 - 题材股
    4: 'rgba(16, 185, 129, 0.6)'    // 绿色 - 强势股
  };
  return colors[quadrant] || 'rgba(100, 116, 139, 0.6)';
};

/**
 * 初始化图表
 */
const initChart = () => {
  if (!chartRef.value) return;
  
  chartInstance = echarts.init(chartRef.value);
  
  // 点击事件
  chartInstance.on('click', (params: any) => {
    if (params.data) {
      selectedStock.value = params.data as BubbleData;
    }
  });
  
  // 监听窗口大小变化
  const resizeHandler = () => chartInstance?.resize();
  window.addEventListener('resize', resizeHandler);
};

/**
 * 更新图表数据
 */
const updateChart = () => {
  if (!chartInstance || !props.data) {
    isEmpty.value = true;
    return;
  }
  
  const { bubbles } = props.data;
  
  if (!bubbles || bubbles.length === 0) {
    isEmpty.value = true;
    chartInstance.clear();
    return;
  }
  
  isEmpty.value = false;
  
  // 按象限分组数据
  const quadrantData: Record<number, BubbleData[]> = { 1: [], 2: [], 3: [], 4: [] };
  bubbles.forEach(b => {
    quadrantData[b.quadrant]?.push(b);
  });
  
  // 创建系列数据
  const series = Object.entries(quadrantData).map(([quadrant, data]) => ({
    name: ['权重股', '跟风股', '题材股', '强势股'][parseInt(quadrant) - 1],
    type: 'scatter',
    data: data.map(b => ({
      value: [b.limit_up_time, b.market_cap, b.order_amount / 1e8, b.continue_num],
      ...b
    })),
    symbolSize: (data: any) => {
      // 气泡大小基于封单额，范围 10-50
      const orderAmount = data.value?.[2] || 0;
      return Math.max(10, Math.min(50, 10 + orderAmount * 2));
    },
    itemStyle: {
      color: getQuadrantBgColor(parseInt(quadrant)),
      shadowBlur: 10,
      shadowColor: 'rgba(0, 0, 0, 0.3)'
    },
    emphasis: {
      itemStyle: {
        shadowBlur: 20,
        borderWidth: 2,
        borderColor: '#fff'
      }
    }
  }));
  
  const option: echarts.EChartsOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: '#334155',
      textStyle: {
        color: '#e2e8f0',
        fontSize: 11
      },
      formatter: (params: any) => {
        const d = params.data;
        return `
          <div class="font-medium">${d.stock_name} (${d.stock_code})</div>
          <div class="text-xs mt-1 space-y-0.5">
            <div>连板: <span class="text-red-400">${d.continue_num}板</span></div>
            <div>市值: ${d.market_cap}亿</div>
            <div>封板: ${d.limit_up_time_str}</div>
            <div>封单: ${(d.order_amount / 1e8).toFixed(2)}亿</div>
          </div>
        `;
      }
    },
    legend: {
      show: false
    },
    grid: {
      left: '8%',
      right: '8%',
      bottom: '12%',
      top: '8%',
      containLabel: true
    },
    xAxis: {
      type: 'value',
      name: '封板时间',
      nameLocation: 'middle',
      nameGap: 30,
      nameTextStyle: {
        color: '#64748b',
        fontSize: 11
      },
      min: 0,
      max: 240,
      interval: 60,
      axisLine: {
        lineStyle: { color: '#334155' }
      },
      axisLabel: {
        color: '#64748b',
        fontSize: 10,
        formatter: (value: number) => {
          // 转换为时间格式
          const hour = 9 + Math.floor((30 + value) / 60);
          const minute = (30 + value) % 60;
          return `${hour}:${minute.toString().padStart(2, '0')}`;
        }
      },
      splitLine: {
        lineStyle: { color: '#1e293b', type: 'dashed' }
      }
    },
    yAxis: {
      type: 'value',
      name: '市值(亿)',
      nameLocation: 'middle',
      nameGap: 45,
      nameTextStyle: {
        color: '#64748b',
        fontSize: 11
      },
      min: 0,
      axisLine: {
        lineStyle: { color: '#334155' }
      },
      axisLabel: {
        color: '#64748b',
        fontSize: 10
      },
      splitLine: {
        lineStyle: { color: '#1e293b', type: 'dashed' }
      }
    },
    series,
    // 添加象限分割线和标注
    graphic: [
      // 垂直分割线（封板时间 60 分钟 = 10:30）
      {
        type: 'line',
        z: 1,
        left: 'center',
        top: 'middle',
        shape: {
          x1: 0,
          y1: 0,
          x2: 0,
          y2: 0
        },
        style: {
          stroke: '#475569',
          lineWidth: 1,
          lineDash: [4, 4]
        }
      },
      // 水平分割线（市值 50 亿）
      {
        type: 'line',
        z: 1,
        left: 'center',
        top: 'middle',
        shape: {
          x1: 0,
          y1: 0,
          x2: 0,
          y2: 0
        },
        style: {
          stroke: '#475569',
          lineWidth: 1,
          lineDash: [4, 4]
        }
      },
      // 象限标注
      {
        type: 'text',
        z: 1,
        left: '15%',
        top: '15%',
        style: {
          text: '权重股',
          fill: '#3b82f6',
          fontSize: 12,
          fontWeight: 'bold'
        }
      },
      {
        type: 'text',
        z: 1,
        left: '75%',
        top: '15%',
        style: {
          text: '跟风股',
          fill: '#f59e0b',
          fontSize: 12,
          fontWeight: 'bold'
        }
      },
      {
        type: 'text',
        z: 1,
        left: '75%',
        top: '80%',
        style: {
          text: '题材股',
          fill: '#ec4899',
          fontSize: 12,
          fontWeight: 'bold'
        }
      },
      {
        type: 'text',
        z: 1,
        left: '15%',
        top: '80%',
        style: {
          text: '强势股',
          fill: '#10b981',
          fontSize: 12,
          fontWeight: 'bold'
        }
      }
    ]
  };
  
  chartInstance.setOption(option, true);
};

// 监听数据变化
watch(
  () => props.data,
  () => {
    nextTick(() => updateChart());
  },
  { deep: true }
);

// 监听加载状态
watch(
  () => props.loading,
  (loading) => {
    if (!loading) {
      nextTick(() => updateChart());
    }
  }
);

onMounted(() => {
  initChart();
  if (props.data) {
    updateChart();
  }
});

onUnmounted(() => {
  chartInstance?.dispose();
  window.removeEventListener('resize', () => chartInstance?.resize());
});
</script>

<style scoped>
.bubble-matrix-chart {
  position: relative;
  background: transparent;
}
</style>
