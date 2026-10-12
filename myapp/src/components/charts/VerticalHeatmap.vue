<template>
  <div class="w-full h-full p-2 overflow-y-auto no-scrollbar">
    <div v-for="yearData in sortedData" :key="yearData.year" class="mb-6">
      <div class="text-[11px] font-bold text-[#787b86] mb-2 px-2 uppercase tracking-tighter">{{ yearData.year }} PERFORMANCE</div>
      <div class="grid grid-cols-3 gap-[1px] bg-[#2a2e39] border border-[#2a2e39]">
        <div 
          v-for="(val, idx) in yearData.months" 
          :key="idx"
          class="h-10 flex flex-col items-center justify-center bg-[#0A0A0A] transition-colors hover:bg-[#1e222d] cursor-pointer"
          @click="emit('monthSelect', { year: yearData.year, month: idx })"
        >
          <span class="text-[9px] text-[#787b86] leading-none mb-1 uppercase">{{ monthsShort[idx] }}</span>
          <span 
            v-if="val !== null"
            class="text-[10px] font-bold font-mono leading-none"
            :class="val >= 0 ? 'text-[#089981]' : 'text-[#f23645]'"
          >
            {{ val >= 0 ? '+' : '' }}{{ val.toFixed(1) }}%
          </span>
          <span v-else class="text-[10px] text-[#2a2e39]">--</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  data: any[];
}>();

const emit = defineEmits(['monthSelect']);

const monthsShort = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

const sortedData = computed(() => {
  return [...props.data].sort((a, b) => b.year - a.year);
});
</script>

<style scoped>
.no-scrollbar::-webkit-scrollbar {
  display: none;
}
</style>
