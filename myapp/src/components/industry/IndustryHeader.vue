<template>
  <header class="industry-header">
    <div class="title-block">
      <h1>产业链雷达</h1>
      <p class="subtitle">数据驱动的产业链可视化研究系统</p>
    </div>

    <div class="summary-cards" :class="{ loading }">
      <div class="card">
        <span class="card-label">当前主题</span>
        <span class="card-value">{{ themeName }}</span>
      </div>
      <div class="card">
        <span class="card-label">产业链结构</span>
        <span class="card-value ok">{{ structureStatus }}</span>
      </div>
      <div class="card">
        <span class="card-label">节点数量</span>
        <span class="card-value">{{ nodeCount }}</span>
      </div>
      <div class="card">
        <span class="card-label">结构来源</span>
        <span class="card-value" :class="usingFallback ? 'warn' : 'ok'">
          {{ usingFallback ? '内置示例' : '本地知识库' }}
        </span>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ChainInfo, ChainSummary } from '@/api/industryChain';

interface Props {
  chain: ChainInfo | null;
  summary: ChainSummary | null;
  loading: boolean;
  usingFallback: boolean;
}

const props = defineProps<Props>();

const themeName = computed(() => props.chain?.name || props.summary?.chain_name || '光通信');
const nodeCount = computed(() => props.summary?.node_count || props.chain?.node_count || 0);
const structureStatus = computed(() => (props.loading ? '加载中' : nodeCount.value > 0 ? '已加载' : '暂无'));
</script>

<style scoped>
.industry-header {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.title-block h1 {
  margin: 0;
  color: #f8fafc;
  font-size: 28px;
  line-height: 1.2;
}

.subtitle {
  margin: 6px 0 0;
  color: #94a3b8;
  font-size: 14px;
}

.summary-cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 12px;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border: 1px solid rgba(71, 85, 105, 0.45);
  border-radius: 8px;
  background: linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(15, 23, 42, 0.72));
}

.card-label {
  color: #64748b;
  font-size: 12px;
}

.card-value {
  color: #e2e8f0;
  font-size: 17px;
  font-weight: 650;
}

.card-value.ok {
  color: #34d399;
}

.card-value.warn {
  color: #fbbf24;
}

.loading .card {
  opacity: 0.72;
}

@media (max-width: 900px) {
  .summary-cards {
    grid-template-columns: repeat(2, minmax(140px, 1fr));
  }
}
</style>
