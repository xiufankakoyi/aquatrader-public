<template>
  <div class="result-table-wrap">
    <table class="result-table">
      <thead>
        <tr>
          <th>股票代码</th>
          <th>股票名称</th>
          <th>命中日期</th>
          <th>分数</th>
          <th>命中原因</th>
          <th>风险标签</th>
          <th>1日</th>
          <th>3日</th>
          <th>5日</th>
          <th>10日</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in results"
          :key="rowKey(item)"
          :class="{ selected: rowKey(item) === selectedKey }"
          @click="$emit('select', item)"
        >
          <td class="mono">{{ item.symbol }}</td>
          <td>{{ item.stock_name || '--' }}</td>
          <td>{{ item.match_date }}</td>
          <td>{{ item.match_score.toFixed(4) }}</td>
          <td>
            <div class="reason-list">{{ item.hit_reasons.slice(0, 2).join('；') }}</div>
          </td>
          <td>
            <template v-if="item.risk_flags.length === 0">
              <span class="muted">无</span>
            </template>
            <template v-else>
              <span v-for="risk in item.risk_flags" :key="risk" class="risk-chip">{{ risk }}</span>
            </template>
          </td>
          <td :class="returnClass(item.future_return_1d)">{{ formatPercent(item.future_return_1d) }}</td>
          <td :class="returnClass(item.future_return_3d)">{{ formatPercent(item.future_return_3d) }}</td>
          <td :class="returnClass(item.future_return_5d)">{{ formatPercent(item.future_return_5d) }}</td>
          <td :class="returnClass(item.future_return_10d)">{{ formatPercent(item.future_return_10d) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="results.length === 0" class="empty">暂无命中样本</div>
  </div>
</template>

<script setup lang="ts">
import type { PatternMatch } from '@/api/pattern';

defineProps<{
  results: PatternMatch[];
  selectedKey?: string;
}>();

defineEmits<{
  select: [match: PatternMatch];
}>();

function rowKey(item: PatternMatch): string {
  return `${item.pattern_id}:${item.symbol}:${item.match_date}`;
}

function formatPercent(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function returnClass(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '';
  return value >= 0 ? 'up' : 'down';
}
</script>

<style scoped>
.result-table-wrap {
  overflow: auto;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0b1120;
}

.result-table {
  width: 100%;
  min-width: 1080px;
  border-collapse: collapse;
}

th,
td {
  padding: 10px 12px;
  border-bottom: 1px solid #1f2937;
  color: #cbd5e1;
  font-size: 12px;
  text-align: left;
  vertical-align: top;
}

th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #111827;
  color: #94a3b8;
  font-weight: 700;
}

tbody tr {
  cursor: pointer;
}

tbody tr:hover,
tbody tr.selected {
  background: rgba(34, 197, 94, 0.08);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.reason-list {
  max-width: 300px;
  line-height: 1.5;
}

.risk-chip {
  display: inline-block;
  margin: 0 4px 4px 0;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.14);
  color: #fbbf24;
}

.muted {
  color: #64748b;
}

.up {
  color: #22c55e;
}

.down {
  color: #ef4444;
}

.empty {
  padding: 28px;
  color: #64748b;
  text-align: center;
}
</style>
