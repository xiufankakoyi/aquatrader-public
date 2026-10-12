<template>
  <div class="bg-[#151925] rounded-lg p-6 border border-slate-800 space-y-6">
    <div class="flex items-center justify-between">
      <h3 class="text-lg font-semibold text-white">持仓分析</h3>
      <div class="text-sm text-slate-400">
        更新时间: {{ formatDate(currentDate) }}
      </div>
    </div>
    
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- 左侧：持仓分布饼图 -->
      <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
        <h4 class="text-md font-medium text-slate-300 mb-4">头寸分布分析</h4>
        <div class="h-72">
          <PositionPieChart :positions="positions" />
        </div>
      </div>
      
      <!-- 右侧：持仓变化趋势图 -->
      <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
        <h4 class="text-md font-medium text-slate-300 mb-4">持仓变化趋势</h4>
        <div class="h-72">
          <div ref="trendChartRef" class="w-full h-full"></div>
        </div>
      </div>
    </div>
    
    <!-- 盈亏归因分析 -->
    <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
      <h4 class="text-md font-medium text-slate-300 mb-4">盈亏归因分析</h4>
      <div class="space-y-4">
        <div v-for="item in attributionData" :key="item.symbol" class="grid grid-cols-4 gap-4 items-center p-3 bg-slate-800/70 rounded-lg border border-slate-700">
          <div class="font-medium text-white">{{ item.symbol }}</div>
          <div class="text-sm text-slate-400">{{ item.contribution.toFixed(2) }}%</div>
          <div class="text-sm" :class="item.profit >= 0 ? 'text-green-400' : 'text-red-400'">
            {{ formatCurrency(item.profit) }}
          </div>
          <div class="text-sm text-slate-400">
            <div class="w-full bg-slate-700 rounded-full h-2">
              <div 
                class="h-full rounded-full transition-all duration-300" 
                :class="item.profit >= 0 ? 'bg-green-500' : 'bg-red-500'"
                :style="{ width: `${Math.abs(item.contribution)}%` }"
              ></div>
            </div>
          </div>
        </div>
        
        <div v-if="attributionData.length === 0" class="flex items-center justify-center h-32 text-slate-500">
          <div class="text-center">
            <i class="fas fa-chart-pie text-4xl mb-2"></i>
            <p>暂无盈亏归因数据</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from 'vue';
import * as echarts from 'echarts';
import type { HoldingPeriod } from '../types/backtest';
import PositionPieChart from './charts/PositionPieChart.vue';

interface Position {
  symbolCode: string;
  symbolName: string;
  quantity: number;
  cost: number;
  currentPrice: number;
  positionValue: number;
  profitLoss: number;
  profitRatio: number;
  availableQuantity: number;
}

interface AttributionItem {
  symbol: string;
  profit: number;
  contribution: number;
}

interface Props {
  holdingPeriods?: HoldingPeriod[];
  currentDate: string;
  latestPrices?: Record<string, number>;
  equityCurve?: Array<{ date: string; equity: number }>;
}

const props = withDefaults(defineProps<Props>(), {
  holdingPeriods: () => [],
  currentDate: () => new Date().toISOString().slice(0, 10),
  latestPrices: () => ({}),
  equityCurve: () => []
});

const trendChartRef = ref<HTMLElement | null>(null);
let trendChart: echarts.ECharts | null = null;

// 统一代码格式
const normalizeSymbolCode = (value?: string): string => {
  if (!value) return '';
  const trimmed = value.trim().toUpperCase();
  const match = trimmed.match(/(\d+)/);
  if (match) {
    // CHANGED: 补齐 6 位，解决 00 开头股票无法查询名称的问题
    const digits = match[1];
    return digits.length < 6 ? digits.padStart(6, '0') : digits;
  }
  return trimmed;
};

// 计算当前持仓
const positions = computed<Position[]>(() => {
  const currentPositions: Position[] = [];
  
  // 筛选出当前持仓（exitDate 为 null 或空）
  const openPositions = props.holdingPeriods.filter(
    hp => !hp.exitDate || hp.exitDate === null
  );
  
  // 按 symbolCode 分组，处理同一标的的多次买入
  const positionMap = new Map<string, {
    symbolCode: string;
    symbolName: string;
    totalQuantity: number;
    totalCost: number;
    entryDates: string[];
  }>();
  
  openPositions.forEach(hp => {
    const rawSymbolCode = hp.symbolCode || '';
    const symbolCode = normalizeSymbolCode(rawSymbolCode);
    const symbolName = hp.symbolName || rawSymbolCode || symbolCode;
    const quantity = hp.quantity || 0;
    const entryPrice = hp.entryPrice || 0;
    const entryDate = hp.entryDate || '';
    
    if (!symbolCode || quantity <= 0) return;
    
    if (positionMap.has(symbolCode)) {
      const existing = positionMap.get(symbolCode)!;
      existing.totalQuantity += quantity;
      existing.totalCost += quantity * entryPrice;
      existing.entryDates.push(entryDate);
    } else {
      positionMap.set(symbolCode, {
        symbolCode,
        symbolName,
        totalQuantity: quantity,
        totalCost: quantity * entryPrice,
        entryDates: [entryDate]
      });
    }
  });
  
  // 转换为 Position 数组
  positionMap.forEach((pos, symbolCode) => {
    const avgCost = pos.totalQuantity > 0 ? pos.totalCost / pos.totalQuantity : 0;
    const currentPrice = props.latestPrices[normalizeSymbolCode(symbolCode)] ?? avgCost;
    const positionValue = pos.totalQuantity * currentPrice;
    const profitLoss = positionValue - pos.totalCost;
    const profitRatio = avgCost > 0 ? (profitLoss / pos.totalCost) * 100 : 0;
    
    currentPositions.push({
      symbolCode,
      symbolName: pos.symbolName,
      quantity: pos.totalQuantity,
      cost: avgCost,
      currentPrice,
      positionValue,
      profitLoss,
      profitRatio,
      availableQuantity: pos.totalQuantity
    });
  });
  
  return currentPositions.sort((a, b) => b.positionValue - a.positionValue);
});

// 盈亏归因数据
const attributionData = computed<AttributionItem[]>(() => {
  const totalProfit = positions.value.reduce((sum, pos) => sum + pos.profitLoss, 0);
  
  if (totalProfit === 0) {
    return positions.value.map(pos => ({
      symbol: pos.symbolCode,
      profit: pos.profitLoss,
      contribution: 0
    }));
  }
  
  return positions.value.map(pos => ({
    symbol: pos.symbolCode,
    profit: pos.profitLoss,
    contribution: (pos.profitLoss / totalProfit) * 100
  })).sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
});

// 初始化趋势图表
const initTrendChart = () => {
  if (!trendChartRef.value) return;
  
  trendChart = echarts.init(trendChartRef.value);
  
  const option: echarts.EChartsOption = {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        let result = `${params[0].name}<br/>`;
        params.forEach((param: any) => {
          result += `${param.marker} ${param.seriesName}: ${param.value}<br/>`;
        });
        return result;
      },
      backgroundColor: 'rgba(26, 31, 46, 0.9)',
      borderColor: '#334155',
      textStyle: {
        color: '#cbd5e1'
      }
    },
    legend: {
      data: ['总市值', '持仓数量'],
      textStyle: {
        color: '#94a3b8'
      },
      top: 0
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: [],
      axisLabel: {
        color: '#94a3b8'
      },
      axisLine: {
        lineStyle: {
          color: '#334155'
        }
      }
    },
    yAxis: [
      {
        type: 'value',
        name: '总市值',
        position: 'left',
        axisLabel: {
          color: '#94a3b8',
          formatter: '¥{value}'
        },
        axisLine: {
          lineStyle: {
            color: '#334155'
          }
        },
        splitLine: {
          lineStyle: {
            color: '#334155',
            type: 'dashed'
          }
        }
      },
      {
        type: 'value',
        name: '持仓数量',
        position: 'right',
        axisLabel: {
          color: '#94a3b8'
        },
        axisLine: {
          lineStyle: {
            color: '#334155'
          }
        },
        splitLine: {
          show: false
        }
      }
    ],
    series: [
      {
        name: '总市值',
        type: 'line',
        data: [],
        smooth: true,
        itemStyle: {
          color: '#60a5fa'
        },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(96, 165, 250, 0.3)' },
            { offset: 1, color: 'rgba(96, 165, 250, 0.1)' }
          ])
        }
      },
      {
        name: '持仓数量',
        type: 'line',
        yAxisIndex: 1,
        data: [],
        smooth: true,
        itemStyle: {
          color: '#34d399'
        }
      }
    ]
  };
  
  trendChart.setOption(option);
};

// 更新趋势图表
const updateTrendChart = () => {
  if (!trendChart || !props.equityCurve) return;
  
  // 简化数据，只显示最近30个数据点
  const simplifiedData = props.equityCurve.slice(-30);
  
  // 生成持仓数量模拟数据（实际应从持仓历史获取）
  const positionCounts = simplifiedData.map((_, index) => Math.floor(Math.random() * 10) + 1);
  
  trendChart.setOption({
    xAxis: {
      data: simplifiedData.map(item => item.date)
    },
    series: [
      {
        data: simplifiedData.map(item => item.equity)
      },
      {
        data: positionCounts
      }
    ]
  });
};

// 格式化货币
function formatCurrency(value: number): string {
  const sign = value >= 0 ? '+' : '';
  return `${sign}¥${Math.abs(value).toFixed(2)}`;
}

// 格式化日期
function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

// 监听窗口大小变化
const handleResize = () => {
  trendChart?.resize();
};

// 生命周期钩子
onMounted(() => {
  nextTick(() => {
    initTrendChart();
    updateTrendChart();
    window.addEventListener('resize', handleResize);
  });
});

// 监听数据变化
watch(() => [props.equityCurve, props.currentDate], () => {
  updateTrendChart();
}, { deep: true });

// 清理资源
const cleanup = () => {
  trendChart?.dispose();
  window.removeEventListener('resize', handleResize);
};

// 组件卸载时清理
onMounted(() => {
  return cleanup;
});
</script>

<style scoped>
/* 确保图表容器正确显示 */
.h-72 {
  height: 18rem;
}
</style>