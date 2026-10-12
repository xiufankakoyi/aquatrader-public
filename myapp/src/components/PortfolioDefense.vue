<!--
  防守仓仓位模块
  展示银行/保险仓位占比、贡献收益
-->
<template>
  <div class="portfolio-defense-container">
    <!-- 银行股仓位 -->
    <div class="mb-6">
      <h3 class="text-lg font-semibold text-gray-800 dark:text-slate-100 mb-4">银行股仓位</h3>
      <div class="bg-gray-50 dark:bg-slate-700 rounded-lg p-4">
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm text-gray-600 dark:text-slate-400">仓位占比</span>
          <span class="text-lg font-bold text-gray-800 dark:text-slate-100">
            {{ bankPositionRatio.toFixed(2) }}%
          </span>
        </div>
        <div class="w-full bg-gray-200 dark:bg-slate-600 rounded-full h-2 mb-2">
          <div
            class="bg-blue-600 h-2 rounded-full transition-all"
            :style="{ width: `${bankPositionRatio}%` }"
          ></div>
        </div>
        <div class="flex items-center justify-between text-sm">
          <span class="text-gray-600 dark:text-slate-400">贡献收益</span>
          <span
            class="font-semibold"
            :class="bankContribution >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'"
          >
            {{ formatCurrency(bankContribution) }}
          </span>
        </div>
      </div>
    </div>

    <!-- 保险股仓位 -->
    <div class="mb-6">
      <h3 class="text-lg font-semibold text-gray-800 dark:text-slate-100 mb-4">保险股仓位</h3>
      <div class="bg-gray-50 dark:bg-slate-700 rounded-lg p-4">
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm text-gray-600 dark:text-slate-400">仓位占比</span>
          <span class="text-lg font-bold text-gray-800 dark:text-slate-100">
            {{ insurancePositionRatio.toFixed(2) }}%
          </span>
        </div>
        <div class="w-full bg-gray-200 dark:bg-slate-600 rounded-full h-2 mb-2">
          <div
            class="bg-green-600 h-2 rounded-full transition-all"
            :style="{ width: `${insurancePositionRatio}%` }"
          ></div>
        </div>
        <div class="flex items-center justify-between text-sm">
          <span class="text-gray-600 dark:text-slate-400">贡献收益</span>
          <span
            class="font-semibold"
            :class="insuranceContribution >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'"
          >
            {{ formatCurrency(insuranceContribution) }}
          </span>
        </div>
      </div>
    </div>

    <!-- 合计 -->
    <div class="bg-blue-50 dark:bg-blue-900/20 rounded-lg p-4">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-slate-300">防守仓合计</span>
        <span class="text-xl font-bold text-gray-800 dark:text-slate-100">
          {{ totalDefenseRatio.toFixed(2) }}%
        </span>
      </div>
      <div class="flex items-center justify-between text-sm">
        <span class="text-gray-600 dark:text-slate-400">合计贡献收益</span>
        <span
          class="font-semibold text-lg"
          :class="totalContribution >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'"
        >
          {{ formatCurrency(totalContribution) }}
        </span>
      </div>
    </div>

    <!-- 详细列表 -->
    <div class="mt-6">
      <h3 class="text-lg font-semibold text-gray-800 dark:text-slate-100 mb-4">持仓明细</h3>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="bg-gray-50 dark:bg-slate-700">
            <tr>
              <th class="px-4 py-2 text-left font-semibold text-gray-700 dark:text-slate-300">标的</th>
              <th class="px-4 py-2 text-left font-semibold text-gray-700 dark:text-slate-300">类型</th>
              <th class="px-4 py-2 text-right font-semibold text-gray-700 dark:text-slate-300">仓位占比</th>
              <th class="px-4 py-2 text-right font-semibold text-gray-700 dark:text-slate-300">贡献收益</th>
            </tr>
          </thead>
          <tbody class="bg-white dark:bg-slate-800">
            <tr
              v-for="item in defenseHoldings"
              :key="item.symbol"
              class="border-b border-gray-200 dark:border-slate-700"
            >
              <td class="px-4 py-2 text-gray-800 dark:text-slate-100">{{ item.symbol }}</td>
              <td class="px-4 py-2">
                <span
                  class="px-2 py-1 text-xs rounded-full"
                  :class="item.type === 'bank' ? 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' : 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'"
                >
                  {{ item.type === 'bank' ? '银行' : '保险' }}
                </span>
              </td>
              <td class="px-4 py-2 text-right text-gray-700 dark:text-slate-300">
                {{ item.ratio.toFixed(2) }}%
              </td>
              <td
                class="px-4 py-2 text-right font-medium"
                :class="item.contribution >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'"
              >
                {{ formatCurrency(item.contribution) }}
              </td>
            </tr>
            <tr v-if="defenseHoldings.length === 0">
              <td colspan="4" class="px-4 py-8 text-center text-gray-500 dark:text-slate-400">
                暂无防守仓持仓
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { Trade } from '../types/backtest';

interface Props {
  trades: Trade[];
}

const props = defineProps<Props>();

// 银行股代码
const bankCodes = new Set([
  '600000', '600015', '600016', '600036', '601998', '601818', '601166',
  '601328', '601398', '601939', '601988', '601288', '601658', '000001'
]);

// 保险股代码
const insuranceCodes = new Set([
  '601318', '601601', '601336', '000750', '000627'
]);

// 计算银行股仓位占比
const bankPositionRatio = computed(() => {
  return calculatePositionRatio('bank');
});

// 计算保险股仓位占比
const insurancePositionRatio = computed(() => {
  return calculatePositionRatio('insurance');
});

// 计算防守仓合计占比
const totalDefenseRatio = computed(() => {
  return bankPositionRatio.value + insurancePositionRatio.value;
});

// 计算银行股贡献收益
const bankContribution = computed(() => {
  return calculateContribution('bank');
});

// 计算保险股贡献收益
const insuranceContribution = computed(() => {
  return calculateContribution('insurance');
});

// 计算合计贡献收益
const totalContribution = computed(() => {
  return bankContribution.value + insuranceContribution.value;
});

// 防守仓持仓明细
const defenseHoldings = computed(() => {
  const holdings = new Map<string, {
    symbol: string;
    type: 'bank' | 'insurance';
    ratio: number;
    contribution: number;
  }>();

  // 统计买入交易
  const buyTrades = props.trades.filter(t => t.action === 'buy');
  const totalBuyAmount = buyTrades.reduce((sum, t) => sum + (t.price * t.quantity), 0);

  if (totalBuyAmount === 0) return [];

  buyTrades.forEach(trade => {
    const symbolCode = (trade.symbolCode || trade.symbol || '').substring(0, 6);
    let type: 'bank' | 'insurance' | null = null;

    if (bankCodes.has(symbolCode)) {
      type = 'bank';
    } else if (insuranceCodes.has(symbolCode)) {
      type = 'insurance';
    }

    if (type) {
      const tradeAmount = trade.price * trade.quantity;
      const ratio = (tradeAmount / totalBuyAmount) * 100;

      // 查找对应的卖出交易计算收益
      const sellTrade = props.trades.find(t =>
        t.action === 'sell' &&
        (t.symbolCode || t.symbol) === (trade.symbolCode || trade.symbol) &&
        t.date >= trade.date
      );

      const contribution = sellTrade ? (sellTrade.profitLoss || 0) : 0;

      const key = `${symbolCode}-${type}`;
      if (holdings.has(key)) {
        const existing = holdings.get(key)!;
        existing.ratio += ratio;
        existing.contribution += contribution;
      } else {
        holdings.set(key, {
          symbol: trade.symbol,
          type,
          ratio,
          contribution
        });
      }
    }
  });

  return Array.from(holdings.values()).sort((a, b) => b.ratio - a.ratio);
});

// 计算仓位占比
function calculatePositionRatio(type: 'bank' | 'insurance'): number {
  const codes = type === 'bank' ? bankCodes : insuranceCodes;
  const buyTrades = props.trades.filter(t => t.action === 'buy');
  
  if (buyTrades.length === 0) return 0;

  let defenseAmount = 0;
  let totalAmount = 0;

  buyTrades.forEach(trade => {
    const symbolCode = (trade.symbolCode || trade.symbol || '').substring(0, 6);
    const tradeAmount = trade.price * trade.quantity;
    totalAmount += tradeAmount;

    if (codes.has(symbolCode)) {
      defenseAmount += tradeAmount;
    }
  });

  return totalAmount > 0 ? (defenseAmount / totalAmount) * 100 : 0;
}

// 计算贡献收益
function calculateContribution(type: 'bank' | 'insurance'): number {
  const codes = type === 'bank' ? bankCodes : insuranceCodes;
  const sellTrades = props.trades.filter(t => t.action === 'sell');

  return sellTrades
    .filter(trade => {
      const symbolCode = (trade.symbolCode || trade.symbol || '').substring(0, 6);
      return codes.has(symbolCode);
    })
    .reduce((sum, trade) => sum + (trade.profitLoss || 0), 0);
}

// 格式化货币
function formatCurrency(value: number): string {
  return `${value >= 0 ? '+' : ''}¥${Math.abs(value).toFixed(2)}`;
}
</script>

<style scoped>
.portfolio-defense-container {
  width: 100%;
}
</style>

