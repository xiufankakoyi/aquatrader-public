<template>
  <div class="error-state" role="alert">
    <header class="error-state__header">
      <span class="error-state__icon" aria-hidden="true">!</span>
      <strong class="error-state__title">{{ title }}</strong>
    </header>

    <p class="error-state__message">{{ message }}</p>

    <p v-if="detail" class="error-state__detail">{{ detail }}</p>

    <dl v-if="hasMeta" class="error-state__meta">
      <template v-if="status !== undefined && status !== null && status !== ''">
        <dt>状态码</dt>
        <dd>{{ status }}</dd>
      </template>
      <template v-if="endpoint">
        <dt>接口</dt>
        <dd class="error-state__endpoint">{{ endpoint }}</dd>
      </template>
      <template v-if="code">
        <dt>错误码</dt>
        <dd>{{ code }}</dd>
      </template>
      <template v-if="lastRequestTime">
        <dt>请求时间</dt>
        <dd>{{ formattedTime }}</dd>
      </template>
    </dl>

    <button
      v-if="retryable"
      type="button"
      class="error-state__retry"
      @click="$emit('retry')"
    >
      重试
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * ErrorState - 通用错误展示卡片
 *
 * 字段与 api.ts 中 ApiFailure 错误结构对齐:
 *  - status / message / endpoint / code / retryable
 *  - detail 用于附加错误明细
 *  - lastRequestTime 用于记录最后一次失败时间
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    title?: string
    message: string
    detail?: string
    endpoint?: string
    status?: number | string
    code?: string
    retryable?: boolean
    lastRequestTime?: string
  }>(),
  {
    title: '加载失败',
    detail: '',
    endpoint: '',
    status: '',
    code: '',
    retryable: true,
    lastRequestTime: '',
  }
)

defineEmits<{ retry: [] }>()

const hasMeta = computed(
  () =>
    (props.status !== '' && props.status !== null && props.status !== undefined) ||
    !!props.endpoint ||
    !!props.code ||
    !!props.lastRequestTime
)

const formattedTime = computed(() => {
  if (!props.lastRequestTime) return ''
  try {
    return new Date(props.lastRequestTime).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return props.lastRequestTime
  }
})
</script>

<style scoped>
.error-state {
  min-height: 140px;
  display: grid;
  gap: 8px;
  padding: 18px 20px;
  text-align: left;
  color: #991b1b;
  background: #fff7f7;
  border: 1px solid #fecaca;
  border-radius: 12px;
}
.error-state__header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.error-state__icon {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #dc2626;
  color: #fff;
  font-weight: 700;
  font-size: 13px;
}
.error-state__title { font-size: 15px; }
.error-state__message { margin: 0; color: #b91c1c; }
.error-state__detail {
  margin: 0;
  padding: 8px 10px;
  background: #fee2e2;
  border-radius: 6px;
  font-size: 12px;
  color: #7f1d1d;
  white-space: pre-wrap;
  word-break: break-word;
}
.error-state__meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 12px;
  color: #7f1d1d;
}
.error-state__meta dt { color: #991b1b; font-weight: 600; }
.error-state__meta dd { margin: 0; color: #7f1d1d; word-break: break-all; }
.error-state__endpoint { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.error-state__retry {
  justify-self: start;
  margin-top: 4px;
  padding: 7px 16px;
  border: 0;
  border-radius: 8px;
  color: white;
  background: #dc2626;
  cursor: pointer;
  font-size: 13px;
}
.error-state__retry:hover { background: #b91c1c; }
</style>
