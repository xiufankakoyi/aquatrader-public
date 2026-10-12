<script setup lang="ts">
/**
 * 数据源状态条
 *
 * 展示产业链数据源健康状态与同步信息。
 */
import type { DataSourceStatus } from '@/api/industryChain';

const props = defineProps<{
  status: DataSourceStatus | null;
  loading: boolean;
  structureLoaded: boolean;
  usingFallback: boolean;
}>();

const lastSyncText = () => {
  if (!props.status?.last_sync) return '未同步';
  try {
    return new Date(props.status.last_sync).toLocaleString('zh-CN');
  } catch {
    return props.status.last_sync;
  }
};

const providerNames = () => {
  if (!props.status?.providers) return [];
  return Object.entries(props.status.providers).map(([key, value]) => ({
    key,
    name: value?.name ?? key,
    healthy: Boolean(value?.healthy ?? value?.available),
  }));
};
</script>

<template>
  <div class="data-source-status-bar">
    <div v-if="loading" class="status-loading">正在检查数据源状态...</div>
    <template v-else>
      <div class="status-row">
        <span class="status-label">结构数据:</span>
        <span :class="['status-value', structureLoaded ? 'ok' : 'missing']">
          {{ structureLoaded ? '已加载' : '未加载' }}
        </span>
      </div>
      <div class="status-row">
        <span class="status-label">最后同步:</span>
        <span class="status-value">{{ lastSyncText() }}</span>
      </div>
      <div v-if="usingFallback" class="status-row fallback">
        <span class="status-label">数据模式:</span>
        <span class="status-value">示例数据</span>
      </div>
      <div v-if="providerNames().length" class="providers">
        <div
          v-for="provider in providerNames()"
          :key="provider.key"
          class="provider-chip"
          :class="{ healthy: provider.healthy }"
        >
          {{ provider.name }}
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.data-source-status-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem 1.5rem;
  padding: 0.625rem 1rem;
  margin-bottom: 1rem;
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 0.5rem;
  font-size: 0.8125rem;
  color: #cbd5e1;
}

.status-row {
  display: flex;
  align-items: center;
  gap: 0.375rem;
}

.status-label {
  color: #94a3b8;
}

.status-value.ok {
  color: #34d399;
}

.status-value.missing {
  color: #f87171;
}

.status-row.fallback .status-value {
  color: #fbbf24;
}

.providers {
  display: flex;
  gap: 0.5rem;
  margin-left: auto;
}

.provider-chip {
  padding: 0.125rem 0.5rem;
  border-radius: 999px;
  background: rgba(248, 113, 113, 0.15);
  color: #f87171;
  border: 1px solid rgba(248, 113, 113, 0.25);
}

.provider-chip.healthy {
  background: rgba(52, 211, 153, 0.15);
  color: #34d399;
  border-color: rgba(52, 211, 173, 0.25);
}
</style>
