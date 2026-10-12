<template>
  <div class="calendar-heatmap">
    <div class="heatmap-header">
      <h3 class="heatmap-title">
        <i class="fas fa-calendar-alt"></i>
        收益日历
      </h3>
      <div class="heatmap-legend">
        <span class="legend-label">亏损</span>
        <div class="legend-scale">
          <div class="legend-item" style="background-color: #7f1d1d;"></div>
          <div class="legend-item" style="background-color: #b91c1c;"></div>
          <div class="legend-item" style="background-color: #dc2626;"></div>
          <div class="legend-item" style="background-color: #f87171;"></div>
          <div class="legend-item" style="background-color: #374151;"></div>
          <div class="legend-item" style="background-color: #4ade80;"></div>
          <div class="legend-item" style="background-color: #22c55e;"></div>
          <div class="legend-item" style="background-color: #16a34a;"></div>
          <div class="legend-item" style="background-color: #15803d;"></div>
        </div>
        <span class="legend-label">盈利</span>
      </div>
    </div>

    <div class="heatmap-container">
      <div class="heatmap-grid">
        <!-- 月份标签 -->
        <div class="months-row">
          <div class="day-label"></div>
          <div v-for="month in months" :key="month" class="month-label">
            {{ month }}
          </div>
        </div>

        <!-- 星期标签 + 数据格子 -->
        <div v-for="(day, dayIndex) in weekDays" :key="day" class="heatmap-row">
          <div class="day-label">{{ day }}</div>
          <div class="cells-row">
            <div
              v-for="cell in getCellsForDay(dayIndex)"
              :key="cell.date"
              class="heatmap-cell"
              :class="getCellClass(cell)"
              :style="getCellStyle(cell)"
              :title="getCellTooltip(cell)"
              @click="$emit('date-select', cell.date)"
            ></div>
          </div>
        </div>
      </div>
    </div>

    <div class="heatmap-summary">
      <div class="summary-item">
        <span class="summary-label">盈利天数</span>
        <span class="summary-value text-up">{{ profitDays }}</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">亏损天数</span>
        <span class="summary-value text-down">{{ lossDays }}</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">最大单日盈利</span>
        <span class="summary-value text-up">{{ formatPercent(maxProfit) }}</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">最大单日亏损</span>
        <span class="summary-value text-down">{{ formatPercent(maxLoss) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

interface DailyReturn {
  date: string;
  return: number;
}

interface Props {
  data: DailyReturn[];
  year?: number;
}

const props = withDefaults(defineProps<Props>(), {
  year: () => new Date().getFullYear(),
});

const emit = defineEmits<{
  'date-select': [date: string];
}>();

const weekDays = ['日', '一', '二', '三', '四', '五', '六'];
const months = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

// 生成日历格子数据
const calendarCells = computed(() => {
  const cells = [];
  const startDate = new Date(props.year, 0, 1);
  const endDate = new Date(props.year, 11, 31);

  for (let d = new Date(startDate); d <= endDate; d.setDate(d.getDate() + 1)) {
    const dateStr = d.toISOString().split('T')[0];
    const returnData = props.data.find(item => item.date === dateStr);

    cells.push({
      date: dateStr,
      dayOfWeek: d.getDay(),
      month: d.getMonth(),
      return: returnData?.return || 0,
      hasData: !!returnData,
    });
  }

  return cells;
});

// 按星期几分组
function getCellsForDay(dayIndex: number) {
  return calendarCells.value.filter(cell => cell.dayOfWeek === dayIndex);
}

// 计算颜色强度
function getCellClass(cell: any) {
  if (!cell.hasData) return 'no-data';
  if (cell.return > 0) return 'profit';
  if (cell.return < 0) return 'loss';
  return 'neutral';
}

function getCellStyle(cell: any) {
  if (!cell.hasData || cell.return === 0) {
    return { backgroundColor: '#374151' };
  }

  const absReturn = Math.abs(cell.return);
  const intensity = Math.min(absReturn / 0.03, 1); // 3% 为最大强度

  if (cell.return > 0) {
    // 盈利 - 绿色渐变
    const colors = ['#4ade80', '#22c55e', '#16a34a', '#15803d'];
    const index = Math.floor(intensity * (colors.length - 1));
    return { backgroundColor: colors[index] };
  } else {
    // 亏损 - 红色渐变
    const colors = ['#f87171', '#dc2626', '#b91c1c', '#7f1d1d'];
    const index = Math.floor(intensity * (colors.length - 1));
    return { backgroundColor: colors[index] };
  }
}

function getCellTooltip(cell: any) {
  if (!cell.hasData) return `${cell.date}: 无数据`;
  const sign = cell.return >= 0 ? '+' : '';
  return `${cell.date}: ${sign}${(cell.return * 100).toFixed(2)}%`;
}

// 统计数据
const profitDays = computed(() => {
  return props.data.filter(d => d.return > 0).length;
});

const lossDays = computed(() => {
  return props.data.filter(d => d.return < 0).length;
});

const maxProfit = computed(() => {
  if (props.data.length === 0) return 0;
  return Math.max(...props.data.map(d => d.return));
});

const maxLoss = computed(() => {
  if (props.data.length === 0) return 0;
  return Math.min(...props.data.map(d => d.return));
});

function formatPercent(value: number): string {
  if (value === undefined || value === null) return '0.00%';
  const sign = value >= 0 ? '+' : '';
  return sign + (value * 100).toFixed(2) + '%';
}
</script>

<style scoped>
.calendar-heatmap {
  background-color: var(--bg-secondary);
  border-radius: 8px;
  padding: 16px;
  border: 1px solid var(--border-color);
}

.heatmap-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.heatmap-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.heatmap-title i {
  color: var(--accent-primary);
}

.heatmap-legend {
  display: flex;
  align-items: center;
  gap: 8px;
}

.legend-label {
  font-size: 10px;
  color: var(--text-muted);
}

.legend-scale {
  display: flex;
  gap: 2px;
}

.legend-item {
  width: 12px;
  height: 12px;
  border-radius: 2px;
}

.heatmap-container {
  overflow-x: auto;
  padding-bottom: 8px;
}

.heatmap-grid {
  min-width: 800px;
}

.months-row {
  display: flex;
  margin-bottom: 4px;
}

.month-label {
  flex: 1;
  text-align: center;
  font-size: 10px;
  color: var(--text-muted);
  min-width: 60px;
}

.heatmap-row {
  display: flex;
  align-items: center;
  height: 14px;
  margin-bottom: 2px;
}

.day-label {
  width: 20px;
  font-size: 9px;
  color: var(--text-muted);
  text-align: center;
}

.cells-row {
  display: flex;
  gap: 2px;
  flex: 1;
}

.heatmap-cell {
  width: 12px;
  height: 12px;
  border-radius: 2px;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
}

.heatmap-cell:hover {
  transform: scale(1.2);
  box-shadow: 0 0 0 2px var(--accent-primary);
  z-index: 10;
}

.heatmap-cell.no-data {
  background-color: #374151;
}

.heatmap-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border-color);
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.summary-label {
  font-size: 10px;
  color: var(--text-muted);
}

.summary-value {
  font-size: 16px;
  font-weight: 600;
  font-family: 'JetBrains Mono', monospace;
}

.text-up {
  color: var(--color-up);
}

.text-down {
  color: var(--color-down);
}

/* 响应式 */
@media (max-width: 767px) {
  .heatmap-summary {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
