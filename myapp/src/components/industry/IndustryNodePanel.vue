<template>
  <aside class="node-panel">
    <div v-if="!node" class="empty-panel">
      <i class="fas fa-mouse-pointer"></i>
      <p>默认节点加载中...</p>
    </div>

    <div v-else-if="loading" class="loading-panel">
      <i class="fas fa-spinner fa-spin"></i>
      <p>加载节点详情...</p>
    </div>

    <div v-else class="panel-content">
      <div class="panel-header">
        <div>
          <h3>{{ node.name }}</h3>
          <p>{{ layerName }}</p>
        </div>
        <span class="node-type">{{ nodeType }}</span>
      </div>

      <section class="panel-section">
        <div class="section-title">节点热度</div>
        <div class="metrics-grid">
          <div class="metric hot">
            <span class="metric-label">hot_score</span>
            <span class="metric-value">{{ formatScore(metrics.hot_score) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">强度</span>
            <span class="metric-value">{{ metrics.market_strength || '很弱' }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">候选数</span>
            <span class="metric-value">{{ metrics.candidate_count || 0 }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">涨停数</span>
            <span class="metric-value">{{ metrics.limit_up_count || 0 }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">平均涨幅</span>
            <span class="metric-value" :class="changeClass(metrics.avg_pct_chg)">{{ formatPercent(metrics.avg_pct_chg) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">成交额</span>
            <span class="metric-value">{{ formatAmount(metrics.total_amount) }}</span>
          </div>
        </div>
      </section>

      <section class="panel-section">
        <div class="section-title">自动匹配板块</div>
        <div v-if="matchedBoards.length" class="keywords">
          <span v-for="board in matchedBoards" :key="board" class="keyword-tag">{{ board }}</span>
        </div>
        <div v-else class="empty-line">暂无本地证据</div>
      </section>

      <section class="panel-section">
        <div class="section-title">简介</div>
        <div class="section-value description">{{ description }}</div>
      </section>

      <section class="panel-section">
        <div class="section-title">关键词</div>
        <div v-if="keywords.length" class="keywords">
          <span v-for="kw in keywords" :key="kw" class="keyword-tag">{{ kw }}</span>
        </div>
        <div v-else class="empty-line">暂无关键词</div>
      </section>

      <section class="panel-section">
        <div class="section-title">上下游</div>
        <div class="node-links">
          <span v-for="item in upstream" :key="item.id" class="node-link">上游 {{ item.name }}</span>
          <span v-for="item in downstream" :key="item.id" class="node-link">下游 {{ item.name }}</span>
        </div>
        <div v-if="!upstream.length && !downstream.length" class="empty-line">暂无上下游节点</div>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ChainNode, NodeDetail } from '@/api/industryChain';

interface Props {
  node: ChainNode | null;
  nodeDetail: NodeDetail | null;
  loading: boolean;
}

const props = defineProps<Props>();

const detailNode = computed(() => props.nodeDetail?.node);
const nodeType = computed(() => detailNode.value?.type || props.node?.type || '节点');
const layerName = computed(() => detailNode.value?.layer_name || props.node?.layer_name || props.node?.layer || '暂无层级');
const description = computed(() => detailNode.value?.description || props.node?.description || '暂无描述');
const keywords = computed(() => detailNode.value?.keywords?.length ? detailNode.value.keywords : props.node?.keywords || []);
const upstream = computed(() => props.nodeDetail?.upstream || []);
const downstream = computed(() => props.nodeDetail?.downstream || []);
const metrics = computed(() => props.nodeDetail?.metrics || props.node || {});
const matchedBoards = computed(() => {
  const set = new Set<string>();
  for (const stock of props.nodeDetail?.stocks || []) {
    for (const item of String(stock.matched_board_name || stock.source_concept_name || '').split(';')) {
      const value = item.trim();
      if (value) set.add(value);
    }
  }
  return Array.from(set).slice(0, 12);
});

function formatScore(value: unknown): string {
  const number = Number(value || 0);
  return number.toFixed(2);
}

function formatPercent(value: unknown): string {
  const number = Number(value || 0);
  return `${number.toFixed(2)}%`;
}

function formatAmount(value: unknown): string {
  const number = Number(value || 0);
  if (number >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
  if (number >= 1e4) return `${(number / 1e4).toFixed(2)}万`;
  return number ? number.toFixed(2) : '暂无';
}

function changeClass(value: unknown): string {
  const number = Number(value || 0);
  if (number > 0) return 'up';
  if (number < 0) return 'down';
  return '';
}
</script>

<style scoped>
.node-panel {
  min-height: 640px;
  max-height: 760px;
  overflow-y: auto;
  padding: 18px;
  border: 1px solid rgba(71, 85, 105, 0.45);
  border-radius: 8px;
  background: #0f172a;
}

.empty-panel,
.loading-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 320px;
  gap: 10px;
  color: #64748b;
  font-size: 14px;
}

.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid #1e293b;
}

.panel-header h3 {
  margin: 0;
  color: #f8fafc;
  font-size: 20px;
  line-height: 1.25;
}

.panel-header p {
  margin: 5px 0 0;
  color: #94a3b8;
  font-size: 12px;
}

.node-type {
  flex-shrink: 0;
  padding: 3px 8px;
  border-radius: 6px;
  background: #1e293b;
  color: #cbd5e1;
  font-size: 11px;
}

.panel-section {
  margin-bottom: 18px;
}

.section-title {
  margin-bottom: 7px;
  color: #64748b;
  font-size: 12px;
}

.section-value {
  color: #e2e8f0;
  font-size: 13px;
  line-height: 1.55;
}

.description {
  color: #cbd5e1;
}

.keywords,
.node-links {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.keyword-tag,
.node-link {
  padding: 4px 8px;
  border-radius: 6px;
  background: #1e293b;
  color: #cbd5e1;
  font-size: 12px;
}

.node-link {
  color: #67e8f9;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.metric {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  padding: 9px;
  border-radius: 6px;
  background: #111827;
}

.metric.hot {
  background: rgba(127, 29, 29, 0.34);
}

.metric-label {
  color: #64748b;
  font-size: 11px;
}

.metric-value {
  color: #e2e8f0;
  font-size: 13px;
  font-weight: 650;
}

.empty-line {
  color: #94a3b8;
  font-size: 12px;
}

.up {
  color: #ef4444;
}

.down {
  color: #22c55e;
}
</style>
