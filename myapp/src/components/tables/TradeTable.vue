<template>
  <div class="trade-table-container flex flex-col h-full overflow-hidden">
    <!-- Tab 切换 -->
    <div class="flex items-center space-x-4 mb-4 shrink-0">
      <button
        @click="activeTab = 'orders'"
        :class="activeTab === 'orders' ? 'bg-indigo-500 text-white' : 'bg-slate-800/50 text-slate-400 hover:bg-slate-700/50'"
        class="px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center space-x-2"
      >
        <span>📋</span>
        <span>委托流水</span>
      </button>
      <button
        @click="activeTab = 'closed'"
        :class="activeTab === 'closed' ? 'bg-indigo-500 text-white' : 'bg-slate-800/50 text-slate-400 hover:bg-slate-700/50'"
        class="px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center space-x-2"
      >
        <span>💰</span>
        <span>平仓记录</span>
      </button>
    </div>

    <!-- 委托流水 Tab -->
    <div v-if="activeTab === 'orders'" class="flex-1 flex flex-col min-h-0 overflow-hidden">
      <div class="bg-slate-800/50 flex items-center border-b border-slate-700 shrink-0">
        <div class="w-[15%] px-4 py-3 text-left text-slate-400 font-semibold text-xs uppercase tracking-wider">时间</div>
        <div class="w-[20%] px-4 py-3 text-left text-slate-400 font-semibold text-xs uppercase tracking-wider">标的</div>
        <div class="w-[12%] px-4 py-3 text-left text-slate-400 font-semibold text-xs uppercase tracking-wider">方向</div>
        <div class="w-[15%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">成交价</div>
        <div class="w-[12%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">数量</div>
        <div class="w-[13%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">手续费</div>
        <div class="w-[13%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">发生金额</div>
      </div>
      
      <RecycleScroller
        ref="ordersScroller"
        class="flex-1 overflow-y-auto custom-scrollbar"
        :items="displayedOrders"
        :item-size="56"
        key-field="id"
        v-slot="{ item: trade }"
      >
        <div
          @click="$emit('trade-select', trade)"
          :class="[
            'flex items-center border-b border-slate-800/50 hover:bg-slate-800/30 cursor-pointer transition-colors',
            isHighlighted(trade) ? 'bg-indigo-500/10 border-l-2 border-l-indigo-500 shadow-[inset_0_0_15px_rgba(99,102,241,0.05)]' : 'border-l-2 border-l-transparent'
          ]"
          style="height: 56px;"
        >
          <div class="w-[15%] px-4 py-2 text-slate-300 text-xs font-mono">{{ trade.date }}</div>
          <div class="w-[20%] px-4 py-2 group/symbol cursor-pointer overflow-hidden">
            <div class="text-slate-300 font-medium truncate group-hover/symbol:text-blue-400 transition-colors text-xs">{{ trade.symbol }}</div>
            <div class="text-[10px] text-slate-500 group-hover/symbol:text-blue-400/80 font-mono">{{ trade.symbolCode }}</div>
          </div>
          <div class="w-[12%] px-4 py-2">
            <span
              class="px-2 py-0.5 text-[10px] rounded-full font-bold uppercase"
              :class="trade.action === 'buy' ? 'bg-red-500/10 text-red-400 border border-red-500/20' : 'bg-green-500/10 text-green-400 border border-green-500/20'"
            >
              {{ trade.action === 'buy' ? '买入' : '卖出' }}
            </span>
          </div>
          <div class="w-[15%] px-4 py-2 text-right text-slate-300 text-xs font-mono">¥{{ trade.price.toFixed(2) }}</div>
          <div class="w-[12%] px-4 py-2 text-right text-slate-500 text-xs font-mono">{{ trade.quantity }}</div>
          <div class="w-[13%] px-4 py-2 text-right text-slate-400 text-xs font-mono">¥{{ (trade.commission || 0).toFixed(2) }}</div>
          <div class="w-[13%] px-4 py-2 text-right text-slate-300 font-medium text-xs font-mono whitespace-nowrap">
            {{ trade.action === 'buy' ? '-' : '+' }}¥{{ (trade.price * trade.quantity).toFixed(2) }}
          </div>
        </div>
      </RecycleScroller>
    </div>

    <!-- 平仓记录 Tab -->
    <div v-if="activeTab === 'closed'" class="flex-1 flex flex-col min-h-0 overflow-hidden">
      <div class="bg-slate-800/50 flex items-center border-b border-slate-700 shrink-0">
        <div class="w-[15%] px-4 py-3 text-left text-slate-400 font-semibold text-xs uppercase tracking-wider">时间</div>
        <div class="w-[18%] px-4 py-3 text-left text-slate-400 font-semibold text-xs uppercase tracking-wider">标的</div>
        <div class="w-[11%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">入场价</div>
        <div class="w-[11%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">出场价</div>
        <div class="w-[10%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">天数</div>
        <div class="w-[12%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">最终盈亏</div>
        <div class="w-[10%] px-4 py-3 text-right text-slate-400 font-semibold text-xs uppercase tracking-wider">收益率</div>
        <div class="w-[13%] px-4 py-3 text-left text-slate-400 font-semibold text-xs uppercase tracking-wider">属性</div>
      </div>
      
      <RecycleScroller
        class="flex-1 overflow-y-auto custom-scrollbar"
        :items="closedTrades"
        :item-size="56"
        key-field="id"
        v-slot="{ item: trade }"
      >
        <div
          @click="$emit('trade-select', trade)"
          class="flex items-center border-b border-slate-800/50 hover:bg-slate-800/30 cursor-pointer transition-colors"
          style="height: 56px;"
        >
          <div class="w-[15%] px-4 py-2 text-slate-300 text-xs font-mono">{{ trade.date }}</div>
          <div class="w-[18%] px-4 py-2 group/symbol cursor-pointer overflow-hidden">
            <div class="text-slate-300 font-medium truncate group-hover/symbol:text-blue-400 transition-colors text-xs">{{ trade.symbol }}</div>
            <div class="text-[10px] text-slate-500 group-hover/symbol:text-blue-400/80 font-mono">{{ trade.symbolCode }}</div>
          </div>
          <div class="w-[11%] px-4 py-2 text-right text-slate-400 text-xs font-mono">
            {{ trade.entry_price ? `¥${trade.entry_price.toFixed(2)}` : '-' }}
          </div>
          <div class="w-[11%] px-4 py-2 text-right text-slate-300 text-xs font-mono">¥{{ trade.price.toFixed(2) }}</div>
          <div class="w-[10%] px-4 py-2 text-right text-slate-400 text-xs font-mono">
            {{ trade.holding_days !== undefined ? `${trade.holding_days}d` : (trade.holdingDays !== undefined ? `${trade.holdingDays}d` : '-') }}
          </div>
          <div 
            class="w-[12%] px-4 py-2 text-right font-medium text-xs font-mono"
            :class="(trade.profitLoss || 0) >= 0 ? 'text-red-400' : 'text-green-400'"
          >
            {{ (trade.profitLoss || 0) >= 0 ? '+' : '' }}¥{{ (trade.profitLoss || 0).toFixed(2) }}
          </div>
          <div 
            class="w-[10%] px-4 py-2 text-right text-xs font-mono"
            :class="(trade.roi || 0) >= 0 ? 'text-red-400' : 'text-green-400'"
          >
            {{ (trade.roi || 0) >= 0 ? '+' : '' }}{{ (trade.roi || 0).toFixed(2) }}%
          </div>
          <div class="w-[13%] px-4 py-2 overflow-hidden">
            <div class="flex flex-wrap gap-1">
              <template v-if="getTradeTags(trade).length > 0">
                <span 
                  v-for="tag in getTradeTags(trade)" 
                  :key="tag.label"
                  :class="['px-1.5 py-0.5 rounded text-[9px] font-bold uppercase whitespace-nowrap', tag.class]"
                >
                  {{ tag.label }}
                </span>
              </template>
              <span v-else class="text-slate-600 text-[10px]">-</span>
            </div>
          </div>
        </div>
      </RecycleScroller>
      
      <div v-if="closedTrades.length === 0" class="flex-1 flex items-center justify-center text-slate-500 text-sm">
        暂无平仓记录
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue';
import type { Trade } from '../../types/backtest';
import { useBacktestStore } from '../../store/backtestStore';

const backtestStore = useBacktestStore();
const ordersScroller = ref<any>(null);

const props = defineProps<{
  trades: Trade[];
  highlightDate?: string;
}>();

const activeTab = ref<'orders' | 'closed'>('closed');

const displayedOrders = computed(() => {
  return [...props.trades]
    .filter(t => t.price > 0 && t.quantity > 0)
    .sort((a, b) => b.date.localeCompare(a.date));
});

const closedTrades = computed(() => {
  return props.trades
    .filter(t => t.action === 'sell' && (t.entry_price || t.entryDate)) 
    .sort((a, b) => b.date.localeCompare(a.date));
});

const topDailyTradeId = computed(() => {
  if (!props.highlightDate || props.trades.length === 0) return null;
  const dailyTrades = props.trades.filter(t => t.date === props.highlightDate);
  if (dailyTrades.length === 0) return null;
  
  return dailyTrades.reduce((prev, curr) => {
    const prevVal = Math.abs(prev.profitLoss || (prev.price * prev.quantity));
    const currVal = Math.abs(curr.profitLoss || (curr.price * curr.quantity));
    return currVal > prevVal ? curr : prev;
  }).id;
});

function isHighlighted(trade: any) {
  return trade.id === topDailyTradeId.value;
}

function getBenchmarkReturn(date: string) {
  const series = backtestStore.benchmarkEquitySeries;
  const index = series.findIndex(p => p.date === date);
  if (index <= 0) return 0;
  const current = series[index].equity;
  const prev = series[index - 1].equity;
  return ((current / prev) - 1) * 100;
}

function getTradeTags(trade: Trade) {
  const tags: Array<{ label: string; class: string }> = [];
  const bReturn = getBenchmarkReturn(trade.date);
  
  if (trade.action === 'sell' && trade.profitLoss !== undefined) {
    const isLoss = trade.profitLoss < 0;
    
    if (isLoss) {
      if (bReturn < -0.5) {
        tags.push({ label: 'Beta 亏损', class: 'bg-orange-500/20 text-orange-400 border border-orange-500/30' });
      } else {
        tags.push({ label: 'Alpha 亏损', class: 'bg-red-500/20 text-red-400 border border-red-500/30' });
      }
    }
  }

  return tags;
}

// Optimized scroll-to-date with virtual scroller
watch(() => props.highlightDate, (newDate) => {
  if (newDate && backtestStore.playbackSnap && activeTab.value === 'orders') {
    const index = displayedOrders.value.findIndex(t => t.date === newDate);
    if (index !== -1) {
      nextTick(() => {
        ordersScroller.value?.scrollToItem(index);
      });
    }
  }
});
</script>

<style scoped>
.trade-table-container {
  height: 100%;
}

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

/* Ensure scroller takes full height of container */
.vue-virtual-scroller {
  height: 100%;
}
</style>