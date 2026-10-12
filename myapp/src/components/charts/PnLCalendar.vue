<template>
  <div class="pnl-calendar-container">
    <div class="overflow-x-auto overflow-y-visible custom-scrollbar">
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
              @click="handleMonthClick(yearData.year, index + 1)"
              class="px-3 py-3 text-center transition-all duration-200"
              :class="[
                getCellClass(value),
                value !== null ? 'cursor-pointer hover:shadow-lg hover:z-10 relative' : ''
              ]"
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

    <!-- 钻取模态框 -->
    <div
      v-if="isModalOpen && selectedMonthData"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm transition-opacity duration-300"
      @click="closeModal"
    >
      <div
        class="bg-[#1e1e2d] border border-gray-700 rounded-xl p-6 w-[90%] max-w-[600px] shadow-2xl transform transition-all duration-300 scale-100 opacity-100"
        @click.stop
      >
        <!-- 标题 -->
        <div class="flex justify-between items-center mb-6">
          <h3 class="text-xl font-bold text-white">
            {{ selectedMonthData.year }}年 {{ selectedMonthData.month }}月 每日收益详情
          </h3>
          <button
            @click="closeModal"
            class="text-gray-400 hover:text-white transition-colors text-2xl leading-none"
          >
            ×
          </button>
        </div>

        <!-- 核心内容：日收益柱状图 -->
        <div v-if="selectedMonthData.dailyReturns.length > 0" class="space-y-4">
          <div class="h-64 relative overflow-x-auto custom-scrollbar">
            <!-- 水平分割线 -->
            <div class="absolute top-1/2 left-0 right-0 border-b border-gray-600 pointer-events-none"></div>
            
            <!-- 柱子容器 -->
            <div class="absolute inset-0 flex gap-1 items-center justify-start px-1">
              <div
                v-for="(day, index) in selectedMonthData.dailyReturns"
                :key="`${day.date}-${index}`"
                class="flex-1 min-w-[6px] h-full relative group"
                @mouseenter="showTooltip($event, day)"
                @mouseleave="hideTooltip"
                @mousemove="updateTooltipPosition($event)"
              >
                <!-- 柱子 -->
                <div
                  class="absolute left-0 right-0 rounded-sm transition-opacity hover:opacity-80"
                  :class="day.value >= 0 ? 'bg-red-500' : 'bg-green-500'"
                  :style="barStyles[index]"
                ></div>
                
                <!-- 可交互区域 -->
                <div class="absolute inset-0 z-10 cursor-crosshair"></div>
              </div>
            </div>
          </div>

          <!-- 底部日期轴 -->
          <div class="flex justify-between text-xs text-gray-500 mt-2">
            <span>{{ formatDate(selectedMonthData.dailyReturns[0]?.date) }}</span>
            <span>{{ formatDate(selectedMonthData.dailyReturns[selectedMonthData.dailyReturns.length - 1]?.date) }}</span>
          </div>

          <!-- 统计信息 -->
          <div class="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-gray-700">
            <div class="text-center">
              <div class="text-xs text-gray-400 mb-1">交易日数</div>
              <div class="text-lg font-semibold text-white">{{ selectedMonthData.dailyReturns.length }}</div>
            </div>
            <div class="text-center">
              <div class="text-xs text-gray-400 mb-1">最大单日涨幅</div>
              <div class="text-lg font-semibold text-red-400">
                {{ maxDailyReturn >= 0 ? '+' : '' }}{{ maxDailyReturn.toFixed(2) }}%
              </div>
            </div>
            <div class="text-center">
              <div class="text-xs text-gray-400 mb-1">最大单日跌幅</div>
              <div class="text-lg font-semibold text-green-400">
                {{ minDailyReturn >= 0 ? '+' : '' }}{{ minDailyReturn.toFixed(2) }}%
              </div>
            </div>
          </div>
        </div>
        <div v-else class="text-center py-8 text-gray-400">
          该月暂无日度收益数据
        </div>
      </div>
    </div>
    
    <!-- 全局共享的 Tooltip (使用 Teleport 传送到 body，避免 transform 影响定位) -->
    <Teleport to="body">
      <div
        v-show="tooltip.visible"
        class="fixed z-[9999] bg-gray-800 text-white text-xs font-bold px-2 py-1 rounded shadow-xl border border-gray-600 pointer-events-none transition-transform duration-75 will-change-transform whitespace-nowrap"
        :style="{
          left: `${tooltip.x}px`,
          top: `${tooltip.y}px`,
          transform: 'translate(-50%, -120%)'
        }"
      >
        {{ tooltip.content }}
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive } from 'vue';
import { useBacktestStore } from '../../store/backtestStore';
import type { MonthlyReturn } from '../../types/backtest';

interface Props {
  data: MonthlyReturn[];
}

const props = defineProps<Props>();
const backtestStore = useBacktestStore();

const monthLabels = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

interface ProcessedYearData {
  year: number;
  months: (number | null)[];
  ytd: number | null;
}

interface DailyReturn {
  date: string;
  value: number;
}

interface SelectedMonthData {
  year: number;
  month: number;
  dailyReturns: DailyReturn[];
}

const isModalOpen = ref(false);
const selectedMonthData = ref<SelectedMonthData | null>(null);

// 全局共享的 Tooltip 状态
const tooltip = reactive({
  visible: false,
  x: 0,
  y: 0,
  content: ''
});

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

// 修复配色：使用半透明背景 + 亮色文字
function getCellClass(value: number | null): string {
  if (value === null) return 'bg-slate-900 text-slate-600';
  
  // 涨：半透明红底 + 亮红字
  if (value >= 10) return 'bg-red-500/20 text-red-400';
  if (value >= 5) return 'bg-red-500/20 text-red-400';
  if (value >= 2) return 'bg-red-500/20 text-red-400';
  if (value > 0) return 'bg-red-500/20 text-red-400';
  
  // 平：灰色
  if (value === 0) return 'bg-slate-800 text-slate-400';
  
  // 跌：半透明绿底 + 亮绿字
  if (value > -2) return 'bg-green-500/20 text-green-400';
  if (value > -5) return 'bg-green-500/20 text-green-400';
  return 'bg-green-500/20 text-green-400';
}

// 计算日度收益
function calculateDailyReturns(year: number, month: number): DailyReturn[] {
  if (backtestStore.equitySeries.length === 0) {
    console.log('equitySeries 为空');
    return [];
  }
  
  const monthStr = month.toString().padStart(2, '0');
  const targetPrefix = `${year}-${monthStr}`;
  
  // 过滤出当月的所有权益数据，并确保按日期排序
  const monthEquityData = backtestStore.equitySeries
    .filter(item => item.date.startsWith(targetPrefix))
    .sort((a, b) => a.date.localeCompare(b.date));
  
  console.log(`找到 ${monthEquityData.length} 条 ${year}-${monthStr} 的数据`);
  
  if (monthEquityData.length < 2) {
    console.log('数据点不足，无法计算日度收益');
    return [];
  }
  
  // 计算每日收益率
  const dailyReturns: DailyReturn[] = [];
  for (let i = 1; i < monthEquityData.length; i++) {
    const prevEquity = monthEquityData[i - 1].equity;
    const currEquity = monthEquityData[i].equity;
    
    // 确保权益值有效
    if (prevEquity <= 0 || currEquity <= 0) {
      console.warn(`无效的权益值: ${monthEquityData[i - 1].date}=${prevEquity}, ${monthEquityData[i].date}=${currEquity}`);
      continue;
    }
    
    const dailyReturn = (currEquity / prevEquity - 1) * 100;
    
    dailyReturns.push({
      date: monthEquityData[i].date,
      value: dailyReturn
    });
  }
  
  console.log(`计算出 ${dailyReturns.length} 个日度收益数据点`);
  return dailyReturns;
}

function handleMonthClick(year: number, month: number) {
  console.log(`点击月份: ${year}-${month}, equitySeries 长度: ${backtestStore.equitySeries.length}`);
  
  const dailyReturns = calculateDailyReturns(year, month);
  
  if (dailyReturns.length === 0) {
    console.warn(`无法计算 ${year}-${month} 的日度收益，可能没有数据`);
    // 可以显示一个提示，但暂时先不弹窗
    return;
  }
  
  console.log(`成功计算 ${dailyReturns.length} 个日度收益数据点`);
  
  selectedMonthData.value = {
    year,
    month,
    dailyReturns
  };
  isModalOpen.value = true;
}

function closeModal() {
  isModalOpen.value = false;
  selectedMonthData.value = null;
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

// 计算所有柱子的样式（一次性计算，避免重复计算）
const barStyles = computed<Partial<Record<string, string>>[]>(() => {
  const monthData = selectedMonthData.value;
  if (!monthData || monthData.dailyReturns.length === 0) {
    return [];
  }
  
  // 找到当月最大绝对值，用于归一化
  const maxAbsValue = Math.max(
    ...monthData.dailyReturns.map(d => Math.abs(d.value))
  );
  
  if (maxAbsValue === 0) {
    return monthData.dailyReturns.map(() => ({ bottom: '50%', height: '0%' }));
  }
  
  // 一次性计算所有柱子的样式
  return monthData.dailyReturns.map(day => {
    if (day.value >= 0) {
      // 正收益：bottom: 50%, height: (value / max) * 50%
      const heightPercent = (day.value / maxAbsValue) * 50;
      return {
        bottom: '50%',
        height: `${heightPercent}%`,
        minHeight: '2px'
      };
    } else {
      // 负收益：top: 50%, height: (abs(value) / max) * 50%
      const heightPercent = (Math.abs(day.value) / maxAbsValue) * 50;
      return {
        top: '50%',
        height: `${heightPercent}%`,
        minHeight: '2px'
      };
    }
  });
});

// 计算最大和最小日收益
const maxDailyReturn = computed(() => {
  if (!selectedMonthData.value || selectedMonthData.value.dailyReturns.length === 0) return 0;
  return Math.max(...selectedMonthData.value.dailyReturns.map(d => d.value));
});

const minDailyReturn = computed(() => {
  if (!selectedMonthData.value || selectedMonthData.value.dailyReturns.length === 0) return 0;
  return Math.min(...selectedMonthData.value.dailyReturns.map(d => d.value));
});

// Tooltip 控制函数
const showTooltip = (e: MouseEvent, day: DailyReturn) => {
  // 获取鼠标在屏幕上的绝对位置
  const { clientX, clientY } = e;
  
  tooltip.x = clientX;
  tooltip.y = clientY;
  tooltip.content = `${formatDate(day.date)}: ${day.value >= 0 ? '+' : ''}${day.value.toFixed(2)}%`;
  tooltip.visible = true;
};

const updateTooltipPosition = (e: MouseEvent) => {
  // 实时更新 tooltip 位置
  if (tooltip.visible) {
    tooltip.x = e.clientX;
    tooltip.y = e.clientY;
  }
};

const hideTooltip = () => {
  tooltip.visible = false;
};
</script>

<style scoped>
.pnl-calendar-container {
  width: 100%;
  overflow: hidden; /* 防止容器本身产生滚动条 */
}

.pnl-calendar-container .overflow-x-auto {
  overflow-x: auto;
  overflow-y: visible;
  /* 确保滚动条始终占用空间，避免布局抖动 */
  scrollbar-gutter: stable;
}

/* 自定义滚动条样式 */
.custom-scrollbar::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

.custom-scrollbar::-webkit-scrollbar-track {
  background: transparent;
}

.custom-scrollbar::-webkit-scrollbar-thumb {
  background: #3f3f46;
  border-radius: 3px;
}

.custom-scrollbar::-webkit-scrollbar-thumb:hover {
  background: #52525b;
}

/* Firefox 滚动条样式 */
.custom-scrollbar {
  scrollbar-width: thin;
  scrollbar-color: #3f3f46 transparent;
}
</style>

