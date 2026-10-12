<template>
  <div class="stock-table-wrap">
    <div v-if="stocks.length === 0" class="empty">暂无本地证据，请维护 stock_concept_mapping.csv 后查看映射。</div>
    <table v-else class="stock-table">
      <thead>
        <tr>
          <th>股票代码</th>
          <th>股票名称</th>
          <th>产业链环节</th>
          <th>正宗性评分</th>
          <th>相关度</th>
          <th>纯度</th>
          <th>证据类型</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="stock in stocks" :key="stock.symbol" @click="$emit('select', stock)">
          <td class="mono">{{ stock.symbol }}</td>
          <td>{{ stock.stock_name || '--' }}</td>
          <td>{{ stock.chain_position || '--' }}</td>
          <td>{{ stock.concept_score.toFixed(4) }}</td>
          <td>{{ formatScore(stock.relevance_score) }}</td>
          <td>{{ formatScore(stock.purity_score) }}</td>
          <td>{{ stock.is_sample ? 'sample' : stock.evidence_type || '--' }}</td>
          <td>
            <button type="button" @click.stop="$emit('openPattern', stock)">查看形态</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import type { ConceptStock } from '@/api/concept';

defineProps<{
  stocks: ConceptStock[];
}>();

defineEmits<{
  select: [stock: ConceptStock];
  openPattern: [stock: ConceptStock];
}>();

function formatScore(value: number): string {
  return Number.isFinite(value) ? value.toFixed(4) : '--';
}
</script>

<style scoped>
.stock-table-wrap {
  overflow: auto;
  border: 1px solid #243244;
  border-radius: 8px;
  background: #0b1120;
}

.stock-table {
  width: 100%;
  min-width: 860px;
  border-collapse: collapse;
}

th,
td {
  padding: 10px 12px;
  border-bottom: 1px solid #1f2937;
  color: #cbd5e1;
  font-size: 12px;
  text-align: left;
}

th {
  color: #94a3b8;
  background: #111827;
}

tbody tr {
  cursor: pointer;
}

tbody tr:hover {
  background: rgba(56, 189, 248, 0.08);
}

button {
  height: 28px;
  padding: 0 10px;
  border: 1px solid #16a34a;
  border-radius: 4px;
  background: rgba(22, 163, 74, 0.12);
  color: #86efac;
  cursor: pointer;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.empty {
  padding: 28px;
  color: #64748b;
  text-align: center;
}
</style>
