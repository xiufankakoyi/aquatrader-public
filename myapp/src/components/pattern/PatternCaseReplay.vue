<template>
  <section class="case-panel">
    <div v-if="!match" class="empty">选择一条扫描结果后查看案例复盘</div>
    <template v-else>
      <header class="case-header">
        <div>
          <h3>{{ match.symbol }} {{ match.stock_name || '' }}</h3>
          <p>{{ match.pattern_name }} / {{ match.match_date }}</p>
        </div>
        <span class="status" :class="statusClass">{{ statusText }}</span>
      </header>

      <PatternKLineChart :rows="rows" :match-date="match.match_date" />

      <div class="section-title">事件标签序列</div>
      <EventSequenceChips :sequence="match.event_sequence" :match-date="match.match_date" />

      <div class="detail-grid">
        <div>
          <div class="section-title">命中原因</div>
          <ul>
            <li v-for="reason in match.hit_reasons" :key="reason">{{ reason }}</li>
          </ul>
        </div>
        <div>
          <div class="section-title">风险标签</div>
          <ul v-if="match.risk_flags.length > 0">
            <li v-for="risk in match.risk_flags" :key="risk">{{ risk }}</li>
          </ul>
          <p v-else class="muted">无风险标签</p>
        </div>
      </div>
      <p v-if="error" class="error">{{ error }}</p>
      <p v-if="loading" class="muted">事件 K 线加载中...</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { PatternEventRow, PatternMatch } from '@/api/pattern';
import { getSymbolEvents } from '@/api/pattern';
import PatternKLineChart from '@/components/charts/PatternKLineChart.vue';
import EventSequenceChips from './EventSequenceChips.vue';

const props = defineProps<{
  match: PatternMatch | null;
}>();

const rows = ref<PatternEventRow[]>([]);
const loading = ref(false);
const error = ref('');

watch(
  () => props.match,
  async (match) => {
    rows.value = [];
    error.value = '';
    if (!match) return;
    loading.value = true;
    try {
      const dates = match.event_sequence.map((item) => item.date).filter(Boolean);
      const startDate = dates[0] || match.match_date;
      const endDate = dates[dates.length - 1] || match.match_date;
      rows.value = await getSymbolEvents(match.symbol, startDate, endDate);
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载案例事件失败';
    } finally {
      loading.value = false;
    }
  },
  { immediate: true }
);

const statusText = computed(() => {
  if (!props.match || props.match.success_label === null || props.match.success_label === undefined) return '近端样本';
  return props.match.success_label ? '成功样本' : '失败样本';
});

const statusClass = computed(() => {
  if (!props.match || props.match.success_label === null || props.match.success_label === undefined) return 'neutral';
  return props.match.success_label ? 'success' : 'failure';
});
</script>

<style scoped>
.case-panel {
  min-height: 420px;
  padding: 16px;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0b1120;
}

.case-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

h3 {
  margin: 0 0 4px;
  color: #f8fafc;
  font-size: 16px;
}

p {
  margin: 0;
  color: #94a3b8;
  font-size: 12px;
}

.status {
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.status.success {
  color: #22c55e;
  background: rgba(34, 197, 94, 0.12);
}

.status.failure {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.12);
}

.status.neutral {
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.12);
}

.section-title {
  margin: 14px 0 8px;
  color: #e2e8f0;
  font-size: 13px;
  font-weight: 700;
}

.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

ul {
  margin: 0;
  padding-left: 18px;
  color: #cbd5e1;
  font-size: 12px;
  line-height: 1.7;
}

.muted,
.empty {
  color: #64748b;
}

.empty {
  display: grid;
  min-height: 380px;
  place-items: center;
}

.error {
  margin-top: 10px;
  color: #ef4444;
}

@media (max-width: 960px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}
</style>
