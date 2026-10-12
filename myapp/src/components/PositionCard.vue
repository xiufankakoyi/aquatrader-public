<template>
  <div class="bg-[#0A0A0A] rounded-lg border border-[#2a2e39] overflow-hidden flex flex-col h-full shadow-2xl">
    <!-- Header -->
    <div class="px-4 py-3 border-b border-[#2a2e39] flex items-center justify-between bg-[#1c202b]">
      <div class="flex items-center gap-2">
        <div class="w-1 h-4 bg-blue-500 rounded-full"></div>
        <h3 class="text-sm font-bold text-[#d1d4dc] uppercase tracking-wider">当前持仓</h3>
        <span v-if="positions.length > 0" class="ml-2 px-1.5 py-0.5 rounded bg-[#2a2e39] text-[10px] text-[#868993] font-mono">
          {{ positions.length }}
        </span>
      </div>
      <div class="flex items-center gap-4 text-[11px] font-medium">
        <div class="flex flex-col items-end">
          <span class="text-[#868993] leading-none mb-0.5">总盈亏</span>
          <span :class="['font-bold leading-none', totalProfitLoss >= 0 ? 'text-[#089981]' : 'text-[#f23645]']">
            {{ totalProfitLoss >= 0 ? '+' : '' }}{{ formatVal(totalProfitLoss) }}
          </span>
        </div>
      </div>
    </div>

    <!-- Content -->
    <div class="flex-1 overflow-y-auto custom-scrollbar">
      <table v-if="positions.length > 0" class="w-full text-left border-collapse table-fixed">
        <thead class="sticky top-0 bg-[#0A0A0A] z-10 shadow-sm">
          <tr class="text-[11px] text-[#868993] uppercase tracking-tight">
            <th class="px-4 py-2 font-medium w-[30%]">标的/数量</th>
            <th class="px-2 py-2 font-medium w-[25%] text-right">成本/现价</th>
            <th class="px-2 py-2 font-medium w-[25%] text-right">市值/盈亏</th>
            <th class="px-4 py-2 font-medium w-[20%] text-right">收益率</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-[#2a2e39]/50">
          <tr 
            v-for="pos in positions" 
            :key="pos.symbolCode"
            class="hover:bg-[#1c202b] transition-colors group cursor-pointer"
            @click="emit('analyze', pos)"
          >
            <!-- 标的与数量 -->
            <td class="px-4 py-3">
              <div class="flex flex-col">
                <span class="text-sm font-bold text-[#d1d4dc] group-hover:text-blue-400 truncate">
                  {{ pos.symbolName }}
                </span>
                <div class="flex items-center gap-1.5 mt-0.5">
                  <span class="text-[10px] font-mono text-[#868993] bg-[#2a2e39]/50 px-1 rounded">
                    {{ pos.symbolCode }}
                  </span>
                  <span class="text-[11px] text-[#d1d4dc] font-medium">
                    {{ pos.quantity.toLocaleString() }}
                  </span>
                </div>
              </div>
            </td>

            <!-- 价格对比 (成本/现价) -->
            <td class="px-2 py-3 text-right">
              <div class="flex flex-col">
                <span class="text-[11px] text-[#868993] leading-tight mb-0.5">
                  {{ pos.cost.toFixed(2) }}
                </span>
                <span class="text-xs font-bold text-[#d1d4dc] leading-tight">
                  {{ pos.currentPrice.toFixed(2) }}
                </span>
              </div>
            </td>

            <!-- 市值与盈亏 -->
            <td class="px-2 py-3 text-right">
              <div class="flex flex-col">
                <span class="text-[11px] text-[#d1d4dc] font-medium opacity-80 leading-tight mb-0.5">
                  {{ formatCompact(pos.positionValue) }}
                </span>
                <span :class="['text-[11px] font-bold leading-tight', pos.profitLoss >= 0 ? 'text-[#089981]' : 'text-[#f23645]']">
                  {{ pos.profitLoss >= 0 ? '+' : '' }}{{ formatVal(pos.profitLoss) }}
                </span>
              </div>
            </td>

            <!-- 收益率 -->
            <td class="px-4 py-3 text-right">
              <div :class="[
                'inline-flex items-center justify-center px-1.5 py-1 rounded-sm text-[11px] font-bold min-w-[54px]',
                pos.profitRatio >= 0 ? 'bg-[#089981]/10 text-[#089981]' : 'bg-[#f23645]/10 text-[#f23645]'
              ]">
                {{ pos.profitRatio >= 0 ? '+' : '' }}{{ pos.profitRatio.toFixed(2) }}%
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- Empty State -->
      <div v-else class="flex flex-col items-center justify-center h-full text-[#5d606b] py-12">
        <svg class="w-12 h-12 mb-3 opacity-20" fill="currentColor" viewBox="0 0 24 24">
          <path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zM7 10h2v7H7v-7zm4-3h2v10h-2V7zm4 6h2v4h-2v-4z"/>
        </svg>
        <p class="text-[13px]">暂无公开持仓数据</p>
        <p class="text-[11px] mt-1 opacity-60">请启动回测以查看实时持仓</p>
      </div>
    </div>

    <!-- Footer Stats -->
    <div v-if="positions.length > 0" class="px-4 py-3 bg-[#1c202b] border-t border-[#2a2e39] flex flex-col gap-3">
      <div class="grid grid-cols-2 gap-4">
        <div class="flex flex-col">
          <span class="text-[10px] text-[#868993] uppercase">持仓总市值</span>
          <span class="text-sm font-bold text-[#d1d4dc]">¥{{ totalValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</span>
        </div>
        <div class="flex flex-col items-end">
          <span class="text-[10px] text-[#868993] uppercase">总平均收益</span>
          <span :class="['text-sm font-bold', avgProfitRatio >= 0 ? 'text-[#089981]' : 'text-[#f23645]']">
            {{ avgProfitRatio >= 0 ? '+' : '' }}{{ avgProfitRatio.toFixed(2) }}%
          </span>
        </div>
      </div>
      
      <!-- New: Cash & Total Account Equity -->
      <div v-if="totalEquity > 0" class="pt-3 border-t border-[#2a2e39]/50 grid grid-cols-2 gap-4">
        <div class="flex flex-col">
          <span class="text-[10px] text-blue-400 uppercase font-bold">账户总净值</span>
          <span class="text-sm font-bold text-[#d1d4dc]">¥{{ totalEquity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}</span>
        </div>
        <div class="flex flex-col items-end">
          <span class="text-[10px] text-amber-500 uppercase font-bold">可用现金 (估算)</span>
          <span :class="['text-sm font-bold', (totalEquity - totalValue) >= 0 ? 'text-[#d1d4dc]' : 'text-[#f23645]']">
            ¥{{ (totalEquity - totalValue).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { HoldingPeriod } from '../types/backtest';

type HoldingPeriodWithPricing = HoldingPeriod & {
  quantity?: number;
  entryPrice?: number;
};

interface Position {
  symbolCode: string;
  symbolName: string;
  quantity: number;
  cost: number;
  currentPrice: number;
  positionValue: number;
  profitLoss: number;
  profitRatio: number;
}

interface Props {
  holdingPeriods?: HoldingPeriodWithPricing[];
  currentDate: string;
  latestPrices?: Record<string, number>;
  totalEquity?: number; // NEW: Passes from parent to sync with chart
  stockNames?: Record<string, string>; // 【修复】添加股票名称映射
}

const props = withDefaults(defineProps<Props>(), {
  holdingPeriods: () => [],
  currentDate: () => '',
  latestPrices: () => ({}),
  totalEquity: 0,
  stockNames: () => ({}) // 【修复】默认值
});

const emit = defineEmits<{
  (e: 'analyze', pos: Position): void;
}>();

/**
 * 统一代码格式：规范为 6 位数字代码 (针对 A 股)
 * 例如 1 -> 000001, 1222 -> 001222
 */
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

/**
 * Normalize date string to YYYY-MM-DD format
 */
const normalizeDate = (dateStr?: string | null): string => {
  if (!dateStr) return '';
  return dateStr.replace(/\//g, '-').split(' ')[0];
};

/**
 * Compare two dates (YYYY-MM-DD format)
 * Returns: -1 if a < b, 0 if a === b, 1 if a > b
 */
const compareDates = (a: string, b: string): number => {
  const dateA = new Date(a);
  const dateB = new Date(b);
  if (dateA < dateB) return -1;
  if (dateA > dateB) return 1;
  return 0;
};

const positions = computed<Position[]>(() => {
  const currentDateStr = normalizeDate(props.currentDate);
  
  const openPositions = props.holdingPeriods.filter(hp => {
    const entryDate = normalizeDate(hp.entryDate);
    const exitDate = normalizeDate(hp.exitDate);
    
    if (!entryDate) return false;
    
    if (compareDates(entryDate, currentDateStr) > 0) {
      return false;
    }
    
    if (exitDate && compareDates(exitDate, currentDateStr) <= 0) {
      return false;
    }
    
    return true;
  });

  const positionMap = new Map<string, {
    symbolCode: string;
    symbolName: string;
    totalQuantity: number;
    totalCost: number;
    entryDate: string;
  }>();

  openPositions.forEach(hp => {
    const rawSymbolCode = hp.symbolCode || '';
    const symbolCode = normalizeSymbolCode(rawSymbolCode);
    // 【修复】优先使用 props.stockNames 中的名称，其次是 holdingPeriods 中的名称
    const symbolName = props.stockNames[symbolCode] || hp.symbolName || rawSymbolCode;
    const quantity = hp.quantity || 0;
    const entryPrice = hp.entryPrice || 0;
    const entryDate = normalizeDate(hp.entryDate) || '';

    if (!symbolCode || quantity <= 0) return;

    if (positionMap.has(symbolCode)) {
      const existing = positionMap.get(symbolCode)!;
      existing.totalQuantity += quantity;
      existing.totalCost += quantity * entryPrice;
    } else {
      positionMap.set(symbolCode, {
        symbolCode,
        symbolName,
        totalQuantity: quantity,
        totalCost: quantity * entryPrice,
        entryDate
      });
    }
  });

  const list: Position[] = [];
  positionMap.forEach((pos, code) => {
    const avgCost = pos.totalQuantity > 0 ? pos.totalCost / pos.totalQuantity : 0;
    const currentPrice = props.latestPrices[code] ?? avgCost;
    
    const positionValue = pos.totalQuantity * currentPrice;
    const profitLoss = positionValue - pos.totalCost;
    const profitRatio = pos.totalCost > 0 ? (profitLoss / pos.totalCost) * 100 : 0;

    list.push({
      symbolCode: code,
      symbolName: pos.symbolName,
      quantity: pos.totalQuantity,
      cost: avgCost,
      currentPrice,
      positionValue,
      profitLoss,
      profitRatio
    });
  });

  return list.sort((a, b) => b.positionValue - a.positionValue);
});

const totalValue = computed(() => positions.value.reduce((sum, p) => sum + p.positionValue, 0));
const totalProfitLoss = computed(() => positions.value.reduce((sum, p) => sum + p.profitLoss, 0));
const totalCost = computed(() => positions.value.reduce((sum, p) => sum + (p.quantity * p.cost), 0));
const avgProfitRatio = computed(() => totalCost.value > 0 ? (totalProfitLoss.value / totalCost.value) * 100 : 0);

function formatVal(val: number) {
  if (Math.abs(val) >= 10000) return (val / 10000).toFixed(2) + '万';
  return val.toFixed(2);
}

function formatCompact(val: number) {
  if (val >= 100000000) return (val / 100000000).toFixed(2) + '亿';
  if (val >= 10000) return (val / 10000).toFixed(2) + '万';
  return val.toFixed(0);
}
</script>

<style scoped>
.custom-scrollbar::-webkit-scrollbar {
  width: 4px;
}
.custom-scrollbar::-webkit-scrollbar-track {
  background: transparent;
}
.custom-scrollbar::-webkit-scrollbar-thumb {
  background: #2a2e39;
  border-radius: 2px;
}
.custom-scrollbar::-webkit-scrollbar-thumb:hover {
  background: #363a45;
}
</style>
