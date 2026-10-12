<template>
  <div class="timeout-state" role="alert">
    <header class="timeout-state__header">
      <span class="timeout-state__icon" aria-hidden="true">⏱</span>
      <strong class="timeout-state__title">请求超时</strong>
    </header>

    <p class="timeout-state__message">
      接口 <code>{{ endpoint || '未知' }}</code> 等待 {{ formattedElapsed }} 秒未返回结果
    </p>

    <p v-if="hasQueryParams" class="timeout-state__detail">
      查询参数:
      <code>{{ paramsString }}</code>
    </p>

    <p class="timeout-state__hint">
      建议: 检查网络/后端状态, 或点击重试再次发起请求
    </p>

    <button type="button" class="timeout-state__retry" @click="$emit('retry')">
      重试
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * TimeoutState - 通用请求超时展示
 *
 * 用于 ApiError.code === 'TIMEOUT' 或 elapsedMs 超过设定阈值时使用,
 * 字段与 api.ts 中超时 ApiError 对齐 (endpoint / detail.timeoutMs / 等)
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    endpoint?: string
    elapsedMs: number
    queryParams?: Record<string, any>
  }>(),
  {
    endpoint: '',
    queryParams: () => ({}),
  }
)

defineEmits<{ retry: [] }>()

const formattedElapsed = computed(() => (props.elapsedMs / 1000).toFixed(1))

const hasQueryParams = computed(
  () => props.queryParams && Object.keys(props.queryParams).length > 0
)

const paramsString = computed(() => {
  try {
    return JSON.stringify(props.queryParams)
  } catch {
    return String(props.queryParams)
  }
})
</script>

<style scoped>
.timeout-state {
  min-height: 140px;
  display: grid;
  gap: 8px;
  padding: 18px 20px;
  text-align: left;
  color: #92400e;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 12px;
}
.timeout-state__header { display: flex; align-items: center; gap: 8px; }
.timeout-state__icon {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #d97706;
  color: #fff;
  font-size: 13px;
}
.timeout-state__title { font-size: 15px; }
.timeout-state__message { margin: 0; }
.timeout-state__detail {
  margin: 0;
  padding: 8px 10px;
  background: #fef3c7;
  border-radius: 6px;
  font-size: 12px;
  word-break: break-all;
}
.timeout-state__hint { margin: 0; color: #b45309; font-size: 12px; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #fef3c7;
  padding: 1px 4px;
  border-radius: 4px;
}
.timeout-state__retry {
  justify-self: start;
  margin-top: 4px;
  padding: 7px 16px;
  border: 0;
  border-radius: 8px;
  color: white;
  background: #d97706;
  cursor: pointer;
  font-size: 13px;
}
.timeout-state__retry:hover { background: #b45309; }
</style>
