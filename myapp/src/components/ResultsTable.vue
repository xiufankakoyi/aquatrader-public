<!--
  交易记录表格组件
  支持排序、筛选、分页
-->
<template>
  <div class="results-table-container">
    <!-- 筛选和搜索 -->
    <div class="mb-4 flex items-center justify-between flex-wrap gap-4">
      <div class="flex items-center space-x-4">
        <input
          v-model="searchText"
          type="text"
          placeholder="搜索标的..."
          class="px-4 py-2 border border-gray-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-gray-800 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <select
          v-model="filterAction"
          class="px-4 py-2 border border-gray-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-gray-800 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">全部动作</option>
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
      </div>
      <div class="text-sm text-gray-600 dark:text-slate-400">
        共 {{ filteredTrades.length }} 条记录
      </div>
    </div>

    <!-- 表格 -->
    <div class="overflow-x-auto">
      <table class="w-full text-sm text-left">
        <thead class="bg-gray-50 dark:bg-slate-700">
          <tr>
            <th
              v-for="column in columns"
              :key="column.key"
              @click="handleSort(column.key)"
              class="px-4 py-3 font-semibold text-gray-700 dark:text-slate-300 cursor-pointer hover:bg-gray-100 dark:hover:bg-slate-600"
            >
              <div class="flex items-center space-x-1">
                <span>{{ column.label }}</span>
                <span v-if="sortColumn === column.key" class="text-blue-500">
                  {{ sortOrder === 'asc' ? '↑' : '↓' }}
                </span>
              </div>
            </th>
          </tr>
        </thead>
        <tbody class="bg-white dark:bg-slate-800">
          <!-- Virtual Scroller for rows -->
          <virtual-scroller
            :items="paginatedTrades"
            :item-height="48"
            v-slot="{ item: trade }"
            class="virtual-scroller"
          >
            <tr
              :key="getTradeKey(trade)"
              @click="handleTradeClick(trade)"
              class="border-b border-gray-200 dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 cursor-pointer transition-colors"
            >
              <td class="px-4 py-3 text-gray-700 dark:text-slate-300">{{ trade.date }}</td>
              <td class="px-4 py-3 font-medium text-blue-600 dark:text-blue-400 hover:underline cursor-pointer">
                {{ trade.symbol }}
              </td>
              <td class="px-4 py-3">
                <span
                  class="px-2 py-1 text-xs rounded-full"
                  :class="trade.action === 'buy' ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' : 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'"
                >
                  {{ trade.action === 'buy' ? '买入' : '卖出' }}
                </span>
              </td>
              <td class="px-4 py-3 text-gray-700 dark:text-slate-300">
                ¥{{ trade.price.toFixed(2) }}
              </td>
              <td class="px-4 py-3 text-gray-700 dark:text-slate-300">{{ trade.quantity }}</td>
              <td class="px-4 py-3 text-gray-700 dark:text-slate-300">
                {{ formatCommission(trade) }}
              </td>
              <td
                class="px-4 py-3 font-medium"
                :class="(trade.profitLoss ?? 0) >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'"
              >
                {{ formatCurrency(trade.profitLoss ?? 0) }}
              </td>
              <td
                class="px-4 py-3 font-medium"
                :class="(trade.cumulativePnL ?? 0) >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'"
              >
                {{ formatCurrency(trade.cumulativePnL ?? 0) }}
              </td>
              <td class="px-4 py-3 text-gray-700 dark:text-slate-300">
                {{ formatHoldingPeriod(trade) }}
              </td>
            </tr>
          </virtual-scroller>
          <tr v-if="paginatedTrades.length === 0">
            <td colspan="9" class="px-4 py-8 text-center text-gray-500 dark:text-slate-400">
              暂无交易记录
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页 -->
    <div v-if="totalPages > 1" class="mt-4 flex items-center justify-between">
      <div class="text-sm text-gray-600 dark:text-slate-400">
        显示 {{ (currentPage - 1) * pageSize + 1 }} - {{ Math.min(currentPage * pageSize, filteredTrades.length) }} 条，共 {{ filteredTrades.length }} 条
      </div>
      <div class="flex items-center space-x-2">
        <button
          @click="currentPage = Math.max(1, currentPage - 1)"
          :disabled="currentPage === 1"
          class="px-4 py-2 border border-gray-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-gray-700 dark:text-slate-300 hover:bg-gray-50 dark:hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          上一页
        </button>
        <span class="px-4 py-2 text-gray-700 dark:text-slate-300">
          第 {{ currentPage }} / {{ totalPages }} 页
        </span>
        <button
          @click="currentPage = Math.min(totalPages, currentPage + 1)"
          :disabled="currentPage === totalPages"
          class="px-4 py-2 border border-gray-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-gray-700 dark:text-slate-300 hover:bg-gray-50 dark:hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          下一页
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { VirtualScroller } from 'vue-virtual-scroller';

// Using defineOptions for component options in <script setup>
import { defineOptions } from 'vue';
defineOptions({
  components: {
    VirtualScroller
  }
});

import type { Trade } from '../types/backtest';

interface Props {
  trades: Trade[];
}

const props = defineProps<Props>();

const emit = defineEmits<{
  'trade-select': [trade: Trade];
}>();

// 表格列定义
const columns = [
  { key: 'date', label: '日期' },
  { key: 'symbol', label: '标的' },
  { key: 'action', label: '动作' },
  { key: 'price', label: '价格' },
  { key: 'quantity', label: '数量' },
  { key: 'commission', label: '手续费' },
  { key: 'profitLoss', label: '盈亏' },
  { key: 'cumulativePnL', label: '累计盈亏' },
  { key: 'holdingPeriod', label: '持有周期' }
];

// 状态
const searchText = ref('');
const filterAction = ref('');
const sortColumn = ref<string>('date');
const sortOrder = ref<'asc' | 'desc'>('desc');
const currentPage = ref(1);
const pageSize = ref(20);

// 筛选后的交易记录
const filteredTrades = computed(() => {
  let result = [...props.trades];

  // 搜索筛选
  if (searchText.value) {
    const search = searchText.value.toLowerCase();
    result = result.filter(trade =>
      trade.symbol.toLowerCase().includes(search) ||
      (trade.symbolCode && trade.symbolCode.toLowerCase().includes(search))
    );
  }

  // 动作筛选
  if (filterAction.value) {
    result = result.filter(trade => trade.action === filterAction.value);
  }

  // 排序
  result.sort((a, b) => {
    const aValue = getColumnValue(a, sortColumn.value);
    const bValue = getColumnValue(b, sortColumn.value);
    
    if (aValue === bValue) return 0;
    
    const comparison = aValue > bValue ? 1 : -1;
    return sortOrder.value === 'asc' ? comparison : -comparison;
  });

  return result;
});

// 分页后的交易记录
const paginatedTrades = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  const end = start + pageSize.value;
  return filteredTrades.value.slice(start, end);
});

// 总页数
const totalPages = computed(() => {
  return Math.ceil(filteredTrades.value.length / pageSize.value);
});

// 获取列值
function getColumnValue(trade: Trade, column: string): any {
  switch (column) {
    case 'date':
      return trade.date;
    case 'symbol':
      return trade.symbol;
    case 'action':
      return trade.action;
    case 'price':
      return trade.price;
    case 'quantity':
      return trade.quantity;
    case 'profitLoss':
      return trade.profitLoss ?? 0;
    case 'cumulativePnL':
      return trade.cumulativePnL ?? 0;
    default:
      return '';
  }
}

// 处理排序
function handleSort(column: string) {
  if (sortColumn.value === column) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc';
  } else {
    sortColumn.value = column;
    sortOrder.value = 'asc';
  }
  currentPage.value = 1; // 重置到第一页
}

// 处理交易点击
function handleTradeClick(trade: Trade) {
  emit('trade-select', trade);
}

// 获取交易唯一键
function getTradeKey(trade: Trade): string {
  return trade.id || `${trade.date}-${trade.symbol}-${trade.action}`;
}

// CHANGED: 格式化手续费（万分之五，不足五元按五元算）
function formatCommission(trade: Trade): string {
  // 如果有手续费字段，使用它；否则计算（万分之五，不足五元按五元算）
  let commission: number;
  if ('commission' in trade && typeof (trade as any).commission === 'number') {
    commission = (trade as any).commission;
  } else {
    const commission_base = (trade.price * trade.quantity) * 0.0005;
    commission = Math.max(commission_base, 5.0);
  }
  return formatCurrency(commission);
}

// CHANGED: 格式化持有周期
function formatHoldingPeriod(trade: Trade): string {
  if (trade.action === 'buy') {
    // 买入时，如果有 entryDate，显示"持有中"或计算持有天数
    if (trade.entryDate && trade.exitDate) {
      const days = Math.floor(
        (new Date(trade.exitDate).getTime() - new Date(trade.entryDate).getTime()) / (1000 * 60 * 60 * 24)
      );
      return `${days} 天`;
    } else if (trade.entryDate) {
      const days = Math.floor(
        (new Date(trade.date).getTime() - new Date(trade.entryDate).getTime()) / (1000 * 60 * 60 * 24)
      );
      return days > 0 ? `${days} 天` : '当日';
    }
    return '-';
  } else {
    // 卖出时，显示从 entryDate 到 exitDate 的持有周期
    if (trade.entryDate && trade.exitDate) {
      const days = Math.floor(
        (new Date(trade.exitDate).getTime() - new Date(trade.entryDate).getTime()) / (1000 * 60 * 60 * 24)
      );
      return `${days} 天`;
    } else if (trade.entryDate) {
      const days = Math.floor(
        (new Date(trade.date).getTime() - new Date(trade.entryDate).getTime()) / (1000 * 60 * 60 * 24)
      );
      return days > 0 ? `${days} 天` : '当日';
    }
    return '-';
  }
}

// 格式化货币
function formatCurrency(value: number): string {
  return `${value >= 0 ? '+' : ''}¥${Math.abs(value).toFixed(2)}`;
}
</script>

<style scoped>
.results-table-container {
  width: 100%;
}
</style>

