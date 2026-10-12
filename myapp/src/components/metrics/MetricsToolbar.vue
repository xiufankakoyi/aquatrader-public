<template>
  <div class="h-10 flex items-center bg-[#0A0A0A] border-b border-[#2a2e39] px-4 space-x-6 overflow-hidden select-none">
    <!-- Status Ticker -->
    <div class="flex items-center space-x-2 border-r border-[#2a2e39] pr-6 h-full">
      <div 
        class="w-2 h-2 rounded-full animate-breathe"
        :class="hasBacktestData ? 'bg-[#089981]' : 'bg-[#eab308]'"
      ></div>
      <span class="text-[10px] font-bold text-[#d1d4dc] uppercase tracking-wider">{{ strategyName }}</span>
    </div>

    <!-- Ticker Tape -->
    <div class="flex items-center space-x-6 flex-1 overflow-hidden">
      <div v-for="item in tickerMetrics" :key="item.label" class="flex items-baseline space-x-2">
        <span class="text-[9px] text-[#787b86] uppercase font-bold">{{ item.label }}</span>
        <span 
          class="text-[13px] font-bold font-mono transition-all duration-75"
          :class="getValueColor(item)"
        >
          {{ formatValue(item) }}
        </span>
        <!-- Delta indicator if hovering -->
        <span v-if="item.isHover" class="text-[9px] text-[#2962ff] font-bold opacity-60">LIVE</span>
      </div>
    </div>

    <!-- Strategy Control (Mini) -->
    <div class="flex items-center space-x-4 pl-6 border-l border-[#2a2e39] h-full">
      <slot name="actions"></slot>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

interface MetricItem {
  label: string;
  value: number | null;
  format: 'percent' | 'number';
  inverse?: boolean;
  isHover?: boolean;
}

const props = defineProps<{
  strategyName: string;
  metrics: {
    totalReturn: number | null;
    sharpeRatio: number | null;
    maxDrawdown: number | null;
    profitFactor: number | null;
    winRate: number | null;
  } | null;
  hoverMetrics?: {
    totalReturn?: number | null;
    equity?: number | null;
  } | null;
  hasBacktestData?: boolean;
}>();

const tickerMetrics = computed<MetricItem[]>(() => {
  const m = props.metrics;
  const h = props.hoverMetrics;
  
  return [
    { 
      label: '累计收益', 
      value: h?.totalReturn !== undefined ? h.totalReturn : m?.totalReturn ?? null, 
      format: 'percent',
      isHover: h?.totalReturn !== undefined
    },
    { 
      label: '夏普比率', 
      value: m?.sharpeRatio ?? null, 
      format: 'number' 
    },
    { 
      label: '最大回撤', 
      value: m?.maxDrawdown ?? null, 
      format: 'percent',
      inverse: true
    },
    { 
      label: '盈亏比', 
      value: m?.profitFactor ?? null, 
      format: 'number' 
    },
    { 
      label: '胜率', 
      value: m?.winRate ?? null, 
      format: 'percent' 
    }
  ];
});

function formatValue(item: MetricItem) {
  if (item.value === null || item.value === undefined) return '--';
  if (item.format === 'percent') {
    return `${item.value >= 0 ? '+' : ''}${item.value.toFixed(2)}%`;
  }
  return item.value.toFixed(2);
}

function getValueColor(item: MetricItem) {
  if (item.value === null || item.value === undefined) return 'text-[#787b86]';
  if (item.isHover) return 'text-[#2962ff]'; // Real-time value highlighted in blue
  
  if (item.label === '最大回撤') return 'text-[#f23645]';

  let isPositive = item.value >= 0;
  if (item.inverse) isPositive = !isPositive;

  return isPositive ? 'text-[#089981]' : 'text-[#f23645]';
}
</script>

<style scoped>
.no-scrollbar::-webkit-scrollbar {
  display: none;
}
.no-scrollbar {
  -ms-overflow-style: none;
  scrollbar-width: none;
}
</style>
