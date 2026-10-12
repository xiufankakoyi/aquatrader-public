<template>
  <div class="stock-table-section">
    <div class="table-header">
      <h3 class="section-title">相关股票候选</h3>
      <div class="table-filters">
        <label class="filter-item">
          <input v-model="localVerifiedOnly" type="checkbox" />
          <span>只看已验证</span>
        </label>
        <label class="filter-item">
          <input v-model="localIncludeCandidates" type="checkbox" />
          <span>显示自动候选</span>
        </label>
        <select v-model="localSortBy" class="sort-select">
          <option value="system_relevance_score">按系统相关度</option>
          <option value="pct_chg">按今日涨幅</option>
          <option value="amount">按成交额</option>
          <option value="main_net_inflow">按主力净流入</option>
        </select>
      </div>
    </div>

    <div v-if="loading" class="loading">
      <i class="fas fa-spinner fa-spin"></i>
      <span>加载中...</span>
    </div>

    <div v-else-if="message" class="empty-message">
      <i class="fas fa-info-circle"></i>
      <span>{{ message }}</span>
    </div>

    <div v-else-if="stocks.length === 0" class="empty-message">
      <i class="fas fa-info-circle"></i>
      <span>自动调度尚未生成产业链数据，请等待后端启动后的自动更新</span>
    </div>

    <div v-else class="table-wrapper">
      <table class="stock-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>命中板块</th>
            <th>命中关键词</th>
            <th>标记</th>
            <th>系统相关度</th>
            <th>今日涨幅</th>
            <th>成交额</th>
            <th>是否涨停</th>
            <th>连板数</th>
            <th>主力净流入</th>
            <th>数据源</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="stock in stocks"
            :key="stock.symbol"
            :class="{ candidate: stock.is_candidate, verified: stock.is_verified, manual: stock.is_manual_override }"
          >
            <td>
              <div class="stock-cell">
                <span class="stock-name">{{ stock.stock_name || '未命名' }}</span>
                <span class="stock-code">{{ stock.symbol }}</span>
              </div>
            </td>
            <td>{{ stock.matched_board_name || stock.source_concept_name || '暂无' }}</td>
            <td>{{ stock.matched_keyword || '暂无' }}</td>
            <td>
              <span class="status-tag" :class="tagClass(stock)">
                {{ tagText(stock) }}
              </span>
            </td>
            <td>
              <span class="score" :class="getScoreClass(stock.system_relevance_score ?? stock.final_score)">
                {{ formatScore(stock.system_relevance_score ?? stock.final_score) }}
              </span>
            </td>
            <td :class="getChangeClass(stock.pct_chg)">{{ formatPercent(stock.pct_chg) }}</td>
            <td>{{ formatAmount(stock.amount) }}</td>
            <td>
              <span class="limit-tag" :class="{ active: stock.is_limit_up }">{{ stock.is_limit_up ? '是' : '否' }}</span>
            </td>
            <td>{{ stock.consecutive_limit_count || 0 }}</td>
            <td :class="getChangeClass(stock.main_net_inflow)">{{ formatAmount(stock.main_net_inflow) }}</td>
            <td>{{ stock.data_source || stock.provider || stock.market_provider || '暂无' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ChainStock } from '@/api/industryChain';

interface Props {
  stocks: ChainStock[];
  message: string;
  loading: boolean;
  includeCandidates: boolean;
  verifiedOnly: boolean;
  sortBy: string;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  (e: 'update:includeCandidates', value: boolean): void;
  (e: 'update:verifiedOnly', value: boolean): void;
  (e: 'update:sortBy', value: string): void;
}>();

const localIncludeCandidates = computed({
  get: () => props.includeCandidates,
  set: (val) => emit('update:includeCandidates', val),
});

const localVerifiedOnly = computed({
  get: () => props.verifiedOnly,
  set: (val) => emit('update:verifiedOnly', val),
});

const localSortBy = computed({
  get: () => props.sortBy,
  set: (val) => emit('update:sortBy', val),
});

function tagText(stock: ChainStock): string {
  if (stock.is_manual_override) return '人工 override';
  if (stock.is_verified) return '已验证';
  return '自动候选';
}

function tagClass(stock: ChainStock): string {
  if (stock.is_manual_override) return 'manual';
  if (stock.is_verified) return 'verified';
  return 'candidate';
}

function getScoreClass(score: number): string {
  if (score >= 0.8) return 'high';
  if (score >= 0.6) return 'medium';
  if (score >= 0.4) return 'low';
  return 'none';
}

function getChangeClass(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  if (value > 0) return 'up';
  if (value < 0) return 'down';
  return '';
}

function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined) return '暂无';
  return Number(value).toFixed(4);
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '暂无';
  return `${Number(value).toFixed(2)}%`;
}

function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '暂无';
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(2)}万`;
  return `${sign}${abs.toFixed(2)}`;
}
</script>

<style scoped>
.stock-table-section {
  margin-top: 16px;
  padding: 16px;
  border: 1px solid rgba(71, 85, 105, 0.45);
  border-radius: 8px;
  background: #0f172a;
}

.table-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.section-title {
  margin: 0;
  font-size: 16px;
  color: #f8fafc;
}

.table-filters {
  display: flex;
  align-items: center;
  gap: 12px;
}

.filter-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #94a3b8;
  cursor: pointer;
}

.sort-select {
  padding: 4px 8px;
  border: 1px solid #334155;
  border-radius: 4px;
  background: #1e293b;
  color: #e2e8f0;
  font-size: 12px;
}

.loading,
.empty-message {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px;
  border: 1px dashed #334155;
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.6);
  color: #94a3b8;
  font-size: 14px;
  text-align: center;
}

.table-wrapper {
  overflow-x: auto;
}

.stock-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.stock-table th {
  padding: 10px 12px;
  text-align: left;
  font-weight: 500;
  color: #64748b;
  border-bottom: 1px solid #1e293b;
  white-space: nowrap;
}

.stock-table td {
  max-width: 240px;
  padding: 10px 12px;
  color: #e2e8f0;
  border-bottom: 1px solid #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.stock-table tr:hover td {
  background: #111827;
}

.stock-table tr.candidate td {
  opacity: 0.9;
}

.stock-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stock-name {
  color: #f8fafc;
  font-weight: 650;
}

.stock-code {
  color: #64748b;
  font-size: 11px;
}

.status-tag,
.limit-tag {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 42px;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
}

.status-tag.verified {
  background: #064e3b;
  color: #34d399;
}

.status-tag.candidate {
  background: #451a03;
  color: #fbbf24;
}

.status-tag.manual {
  background: #1e3a8a;
  color: #93c5fd;
}

.limit-tag {
  background: #1e293b;
  color: #94a3b8;
}

.limit-tag.active {
  background: #7f1d1d;
  color: #fecaca;
}

.score {
  font-weight: 650;
}

.score.high {
  color: #22c55e;
}

.score.medium {
  color: #f59e0b;
}

.score.low {
  color: #ef4444;
}

.score.none {
  color: #64748b;
}

.up {
  color: #ef4444;
}

.down {
  color: #22c55e;
}
</style>
