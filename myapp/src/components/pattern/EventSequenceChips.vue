<template>
  <div class="event-sequence">
    <div v-for="item in sequence" :key="item.date" class="event-day" :class="{ matched: item.date === matchDate }">
      <div class="event-date">{{ item.date.slice(5) }}</div>
      <div class="event-change" :class="(item.change_pct || 0) >= 0 ? 'up' : 'down'">
        {{ formatPercent(item.change_pct) }}
      </div>
      <div class="chips">
        <span v-for="event in item.events" :key="event" class="chip">{{ labelEvent(event) }}</span>
        <span v-if="item.events.length === 0" class="chip muted">无标签</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { EventSequenceItem } from '@/api/pattern';

defineProps<{
  sequence: EventSequenceItem[];
  matchDate?: string;
}>();

const EVENT_LABELS: Record<string, string> = {
  strong_attack_day: '强攻击',
  first_divergence_day: '首分歧',
  weak_acceptance_day: '弱承接',
  counterattack_day: '反包',
  break_board_day: '断板',
  volume_burst: '放量',
  volume_shrink: '缩量',
  high_open_low_close: '冲高回落',
  long_upper_shadow: '长上影',
  close_near_high: '近高收',
  close_near_low: '近低收',
  is_limit_up: '涨停',
  is_big_up: '大阳',
  is_big_down: '大跌',
  above_ma5: 'MA5上',
  below_ma5: 'MA5下',
  new_high_20d: '20日新高',
};

function labelEvent(event: string): string {
  return EVENT_LABELS[event] || event;
}

function formatPercent(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}
</script>

<style scoped>
.event-sequence {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
  gap: 8px;
}

.event-day {
  min-height: 88px;
  padding: 9px;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0f172a;
}

.event-day.matched {
  border-color: #22c55e;
  box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.2);
}

.event-date {
  color: #e2e8f0;
  font-size: 12px;
  font-weight: 700;
}

.event-change {
  margin: 4px 0;
  font-size: 12px;
}

.event-change.up {
  color: #22c55e;
}

.event-change.down {
  color: #ef4444;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.chip {
  padding: 2px 5px;
  border-radius: 4px;
  background: rgba(56, 189, 248, 0.14);
  color: #bae6fd;
  font-size: 11px;
}

.chip.muted {
  color: #64748b;
  background: #111827;
}
</style>
