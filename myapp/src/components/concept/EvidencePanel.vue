<template>
  <section class="evidence-panel">
    <div v-if="!stock" class="empty">选择股票后查看本地证据</div>
    <template v-else>
      <h3>{{ stock.symbol }} {{ stock.stock_name }}</h3>
      <div class="score">{{ stock.concept_score.toFixed(4) }}</div>
      <dl>
        <dt>产业链环节</dt>
        <dd>{{ stock.chain_position || '--' }}</dd>
        <dt>证据类型</dt>
        <dd>{{ stock.is_sample ? 'sample' : stock.evidence_type || '--' }}</dd>
        <dt>证据来源</dt>
        <dd>{{ stock.evidence_source || '--' }}</dd>
        <dt>证据文本</dt>
        <dd>{{ stock.evidence_text || '暂无本地证据文本' }}</dd>
        <dt>备注</dt>
        <dd>{{ stock.notes || '--' }}</dd>
      </dl>
    </template>
  </section>
</template>

<script setup lang="ts">
import type { ConceptStock } from '@/api/concept';

defineProps<{
  stock: ConceptStock | null;
}>();
</script>

<style scoped>
.evidence-panel {
  min-height: 280px;
  padding: 16px;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0b1120;
}

h3 {
  margin: 0 0 8px;
  color: #f8fafc;
}

.score {
  display: inline-block;
  margin-bottom: 12px;
  padding: 4px 8px;
  border-radius: 4px;
  background: rgba(34, 197, 94, 0.12);
  color: #86efac;
  font-weight: 700;
}

dl {
  display: grid;
  grid-template-columns: 86px 1fr;
  gap: 8px 12px;
  margin: 0;
}

dt {
  color: #64748b;
  font-size: 12px;
}

dd {
  margin: 0;
  color: #cbd5e1;
  font-size: 12px;
  line-height: 1.6;
}

.empty {
  display: grid;
  min-height: 240px;
  place-items: center;
  color: #64748b;
}
</style>
