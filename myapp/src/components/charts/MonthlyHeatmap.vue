<template>
  <div class="monthly-heatmap">
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr>
            <th class="px-4 py-2 text-left text-slate-400 font-semibold">年份</th>
            <th v-for="month in monthLabels" :key="month" class="px-3 py-2 text-center text-slate-400 font-semibold">
              {{ month }}
            </th>
            <th class="px-4 py-2 text-center text-slate-400 font-semibold">年初至今</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="yearData in processedData" :key="yearData.year" class="border-t border-slate-800">
            <td class="px-4 py-3 text-white font-medium">{{ yearData.year }}</td>
            <td
              v-for="(value, index) in yearData.months"
              :key="index"
              @click="handleMonthClick(yearData.year, index)"
              class="px-3 py-3 text-center cursor-pointer transition-all hover:shadow-lg"
              :class="getCellClass(value)"
            >
              {{ value !== null ? `${value >= 0 ? '+' : ''}${value.toFixed(1)}%` : '-' }}
            </td>
            <td class="px-4 py-3 text-center font-semibold" :class="getCellClass(yearData.ytd)">
              {{ yearData.ytd !== null ? `${yearData.ytd >= 0 ? '+' : ''}${yearData.ytd.toFixed(1)}%` : '-' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { MonthlyReturn } from '../../types/backtest';

interface Props {
  data: MonthlyReturn[];
}

const props = defineProps<Props>();

const monthLabels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

interface ProcessedYearData {
  year: number;
  months: (number | null)[];
  ytd: number | null;
}

const processedData = computed<ProcessedYearData[]>(() => {
  const yearMap = new Map<number, (number | null)[]>();
  
  props.data.forEach(item => {
    if (item.year && item.months) {
      if (!yearMap.has(item.year)) {
        yearMap.set(item.year, Array(12).fill(null));
      }
      const months = yearMap.get(item.year)!;
      item.months.forEach((value, index) => {
        if (value !== null && value !== undefined) {
          months[index] = value;
        }
      });
    }
  });
  
  return Array.from(yearMap.entries())
    .map(([year, months]) => {
      const validMonths = months.filter(v => v !== null) as number[];
      const ytd = validMonths.length > 0 
        ? validMonths.reduce((sum, v) => {
            const monthlyFactor = 1 + (v / 100);
            return sum * monthlyFactor;
          }, 1) - 1
        : null;
      
      return {
        year,
        months,
        ytd: ytd !== null ? ytd * 100 : null
      };
    })
    .sort((a, b) => b.year - a.year);
});

function getCellClass(value: number | null): string {
  if (value === null) return 'bg-slate-900 text-slate-600';
  if (value >= 10) return 'bg-red-900 text-red-300';
  if (value >= 5) return 'bg-red-800 text-red-200';
  if (value >= 2) return 'bg-red-700/50 text-red-100';
  if (value > 0) return 'bg-orange-900/30 text-orange-300';
  if (value === 0) return 'bg-slate-800 text-slate-400';
  if (value > -2) return 'bg-green-900/30 text-green-300';
  if (value > -5) return 'bg-green-800 text-green-200';
  return 'bg-green-900 text-green-300';
}

function handleMonthClick(year: number, monthIndex: number) {
  // 可以触发切换到日收益热图
  console.log('Month clicked:', year, monthIndex);
}
</script>

<style scoped>
.monthly-heatmap {
  width: 100%;
  overflow: hidden; /* 防止容器本身产生滚动条 */
}

.monthly-heatmap .overflow-x-auto {
  overflow-x: auto;
  overflow-y: visible;
  /* 确保滚动条始终占用空间，避免布局抖动 */
  scrollbar-gutter: stable;
}
</style>

