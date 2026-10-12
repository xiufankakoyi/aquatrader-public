<template>
  <div class="bg-[#151925] rounded-lg p-6 border border-slate-800">
    <div class="flex items-center justify-between mb-6">
      <h3 class="text-lg font-semibold text-white">交易记录</h3>
      <div class="text-sm text-slate-400">
        共 {{ filteredTrades.length }} 笔交易
      </div>
    </div>
    
    <!-- 筛选条件 -->
    <div class="mb-6 bg-slate-800/50 rounded-lg p-4 border border-slate-700">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
        <!-- 时间筛选 -->
        <div>
          <label class="block text-sm font-medium text-slate-300 mb-2">时间范围</label>
          <select
            v-model="filter.timeRange"
            class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
          >
            <option value="all">全部时间</option>
            <option value="1m">最近1个月</option>
            <option value="3m">最近3个月</option>
            <option value="6m">最近6个月</option>
            <option value="1y">最近1年</option>
          </select>
        </div>
        
        <!-- 品种筛选 -->
        <div>
          <label class="block text-sm font-medium text-slate-300 mb-2">品种</label>
          <select
            v-model="filter.symbol"
            class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
          >
            <option value="">全部品种</option>
            <option v-for="symbol in uniqueSymbols" :key="symbol" :value="symbol">
              {{ symbol }}
            </option>
          </select>
        </div>
        
        <!-- 交易类型筛选 -->
        <div>
          <label class="block text-sm font-medium text-slate-300 mb-2">交易类型</label>
          <select
            v-model="filter.tradeType"
            class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
          >
            <option value="">全部类型</option>
            <option value="buy">买入</option>
            <option value="sell">卖出</option>
          </select>
        </div>
        
        <!-- 盈亏状态筛选 -->
        <div>
          <label class="block text-sm font-medium text-slate-300 mb-2">盈亏状态</label>
          <select
            v-model="filter.profitStatus"
            class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
          >
            <option value="">全部状态</option>
            <option value="profit">盈利</option>
            <option value="loss">亏损</option>
            <option value="break">盈亏平衡</option>
          </select>
        </div>
      </div>
    </div>
    
    <!-- 交易记录表格 -->
    <div class="overflow-x-auto">
      <table class="w-full">
        <thead>
          <tr class="bg-slate-800/50">
            <th class="px-5 py-3 text-left text-sm font-medium text-slate-400">日期</th>
            <th class="px-5 py-3 text-left text-sm font-medium text-slate-400">品种</th>
            <th class="px-5 py-3 text-left text-sm font-medium text-slate-400">交易类型</th>
            <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">价格</th>
            <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">数量</th>
            <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">金额</th>
            <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">盈亏</th>
            <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">收益率</th>
            <th class="px-5 py-3 text-right text-sm font-medium text-slate-400">持有天数</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800">
          <tr v-for="trade in filteredTrades" :key="trade.id" class="hover:bg-slate-800/30">
            <td class="px-5 py-3 text-sm text-slate-300">{{ trade.date }}</td>
            <td class="px-5 py-3 text-sm text-slate-300 font-medium">{{ trade.symbol }}</td>
            <td class="px-5 py-3">
              <span
                :class="{
                  'bg-green-500/20 text-green-400': trade.action === 'buy',
                  'bg-red-500/20 text-red-400': trade.action === 'sell'
                }"
                class="px-2 py-1 text-xs rounded-full font-medium"
              >
                {{ trade.action === 'buy' ? '买入' : '卖出' }}
              </span>
            </td>
            <td class="px-5 py-3 text-sm text-right text-slate-300">¥{{ trade.price.toFixed(2) }}</td>
            <td class="px-5 py-3 text-sm text-right text-slate-300">{{ formatQuantity(trade.quantity) }}</td>
            <td class="px-5 py-3 text-sm text-right text-slate-300">¥{{ (trade.price * trade.quantity).toFixed(2) }}</td>
            <td class="px-5 py-3 text-sm text-right">
              <span :class="getProfitColor(trade.profitLoss)">
                {{ formatCurrency(trade.profitLoss) }}
              </span>
            </td>
            <td class="px-5 py-3 text-sm text-right">
              <span :class="getProfitColor(trade.roi ?? trade.profitRatio ?? 0)">
                {{ formatPercent(trade.roi ?? trade.profitRatio ?? 0) }}
              </span>
            </td>
            <td class="px-5 py-3 text-sm text-right text-slate-400">{{ trade.holdingDays || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    
    <!-- 空状态 -->
    <div v-if="filteredTrades.length === 0" class="flex items-center justify-center h-32 text-slate-500 mt-4">
      <div class="text-center">
        <i class="fas fa-exchange-alt text-4xl mb-2"></i>
        <p>暂无交易记录</p>
      </div>
    </div>
    
    <!-- 交易统计 -->
    <div v-if="filteredTrades.length > 0" class="mt-6 grid grid-cols-1 md:grid-cols-4 gap-4">
      <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
        <div class="text-sm text-slate-400 mb-1">总交易次数</div>
        <div class="text-2xl font-bold text-white">{{ filteredTrades.length }}</div>
      </div>
      <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
        <div class="text-sm text-slate-400 mb-1">买入次数</div>
        <div class="text-2xl font-bold text-green-400">{{ buyCount }}</div>
      </div>
      <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
        <div class="text-sm text-slate-400 mb-1">卖出次数</div>
        <div class="text-2xl font-bold text-red-400">{{ sellCount }}</div>
      </div>
      <div class="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
        <div class="text-sm text-slate-400 mb-1">总盈亏</div>
        <div class="text-2xl font-bold" :class="totalProfit >= 0 ? 'text-green-400' : 'text-red-400'">
          {{ formatCurrency(totalProfit) }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import type { Trade } from '../types/backtest';

interface FilterOptions {
  timeRange: string;
  symbol: string;
  tradeType: string;
  profitStatus: string;
}

interface Props {
  trades?: Trade[];
}

const props = withDefaults(defineProps<Props>(), {
  trades: () => []
});

const filter = ref<FilterOptions>({
  timeRange: 'all',
  symbol: '',
  tradeType: '',
  profitStatus: ''
});

// 提取所有唯一品种
const uniqueSymbols = computed(() => {
  const symbols = new Set<string>();
  props.trades.forEach(trade => {
    if (trade.symbol) {
      symbols.add(trade.symbol);
    }
  });
  return Array.from(symbols).sort();
});

// 过滤交易记录
const filteredTrades = computed(() => {
  return props.trades.filter(trade => {
    // 时间筛选
    if (filter.value.timeRange !== 'all') {
      const tradeDate = new Date(trade.date || '');
      const now = new Date();
      let cutoffDate = new Date();
      
      switch (filter.value.timeRange) {
        case '1m':
          cutoffDate.setMonth(now.getMonth() - 1);
          break;
        case '3m':
          cutoffDate.setMonth(now.getMonth() - 3);
          break;
        case '6m':
          cutoffDate.setMonth(now.getMonth() - 6);
          break;
        case '1y':
          cutoffDate.setFullYear(now.getFullYear() - 1);
          break;
      }
      
      if (tradeDate < cutoffDate) {
        return false;
      }
    }
    
    // 品种筛选
    if (filter.value.symbol && trade.symbol !== filter.value.symbol) {
      return false;
    }
    
    // 交易类型筛选
    if (filter.value.tradeType && trade.action !== filter.value.tradeType) {
      return false;
    }
    
    // 盈亏状态筛选
    if (filter.value.profitStatus) {
      const profit = trade.profitLoss || 0;
      switch (filter.value.profitStatus) {
        case 'profit':
          if (profit <= 0) return false;
          break;
        case 'loss':
          if (profit >= 0) return false;
          break;
        case 'break':
          if (Math.abs(profit) > 0.01) return false;
          break;
      }
    }
    
    return true;
  }).sort((a, b) => {
    // 按日期倒序排列
    return new Date(b.date || '').getTime() - new Date(a.date || '').getTime();
  });
});

// 统计信息
const buyCount = computed(() => {
  return filteredTrades.value.filter(trade => trade.action === 'buy').length;
});

const sellCount = computed(() => {
  return filteredTrades.value.filter(trade => trade.action === 'sell').length;
});

const totalProfit = computed(() => {
  return filteredTrades.value.reduce((sum, trade) => sum + (trade.profitLoss || 0), 0);
});

// 格式化货币
function formatCurrency(value: number): string {
  const sign = value >= 0 ? '+' : '';
  return `${sign}¥${Math.abs(value).toFixed(2)}`;
}

// 格式化百分比
function formatPercent(value: number): string {
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

// 格式化数量
function formatQuantity(value: number): string {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}

// 获取盈亏颜色
function getProfitColor(value: number): string {
  return value >= 0 ? 'text-green-400' : 'text-red-400';
}
</script>

<style scoped>
/* 确保表格行高正确 */
tbody tr {
  transition: background-color 0.2s ease;
}
</style>