<template>
  <div class="loading-state" role="status">
    <span class="loading-state__spinner" aria-hidden="true"></span>
    <strong class="loading-state__title">{{ title }}</strong>
    <small v-if="description" class="loading-state__description">{{ description }}</small>
    <small v-if="elapsedMs !== undefined" class="loading-state__elapsed">
      已等待 {{ formattedElapsed }} 秒{{ timeoutHint }}
    </small>
  </div>
</template>

<script setup lang="ts">
/**
 * LoadingState - 通用加载态展示
 *
 *  - title / description: 文案
 *  - elapsedMs: 已等待毫秒, 提供时显示
 *  - timeoutMs: 后端/接口超时阈值, 超过会触发 ApiError
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    title?: string
    description?: string
    elapsedMs?: number
    timeoutMs?: number
  }>(),
  {
    title: '正在加载',
    description: '',
    elapsedMs: undefined,
    timeoutMs: 15000,
  }
)

const formattedElapsed = computed(() => {
  const ms = props.elapsedMs ?? 0
  return (ms / 1000).toFixed(1)
})

const timeoutHint = computed(() => {
  if (!props.timeoutMs || props.timeoutMs <= 0) return ''
  if ((props.elapsedMs ?? 0) > props.timeoutMs) {
    return '，超过设定阈值'
  }
  return `，超过 ${(props.timeoutMs / 1000).toFixed(0)}s 会自动 timeout`
})
</script>

<style scoped>
.loading-state {
  min-height: 140px;
  display: grid;
  place-content: center;
  gap: 8px;
  padding: 18px 20px;
  text-align: center;
  color: #475569;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
}
.loading-state__title { font-size: 15px; color: #334155; }
.loading-state__description { color: #64748b; font-size: 13px; }
.loading-state__elapsed { color: #2563eb; font-size: 12px; }
.loading-state__spinner {
  width: 26px;
  height: 26px;
  margin: auto;
  border: 3px solid #dbeafe;
  border-top-color: #2563eb;
  border-radius: 50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
