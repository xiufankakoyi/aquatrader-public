<template>
  <Teleport to="body">
    <div v-if="modelValue" class="api-debug-panel" role="dialog" aria-label="API Debug Panel">
      <header class="api-debug-panel__header">
        <strong>API 调试面板</strong>
        <button
          type="button"
          class="api-debug-panel__close"
          aria-label="关闭"
          @click="close"
        >
          ×
        </button>
      </header>

      <section class="api-debug-panel__badges" aria-label="数据状态徽标">
        <span class="badge" :class="dataDateBadgeClass">
          <span class="badge__label">dataDate</span>
          <span class="badge__value">{{ dataDateDisplay }}</span>
        </span>
        <span class="badge" :class="badgeClass(demoMode)">
          <span class="badge__label">demo_mode</span>
          <span class="badge__value">{{ formatBool(demoMode) }}</span>
        </span>
        <span class="badge" :class="badgeClass(stale)">
          <span class="badge__label">stale</span>
          <span class="badge__value">{{ formatBool(stale) }}</span>
        </span>
        <span class="badge" :class="badgeClass(partialSuccess)">
          <span class="badge__label">partial_success</span>
          <span class="badge__value">{{ formatBool(partialSuccess) }}</span>
        </span>
        <span class="badge badge--count">缓冲: {{ bufferSize }}/100</span>
      </section>

      <section class="api-debug-panel__list" aria-live="polite">
        <div v-if="entries.length === 0" class="api-debug-panel__empty">
          暂无请求记录
        </div>
        <article
          v-for="entry in entries"
          :key="entry.id"
          class="api-debug-panel__entry"
          :class="{ 'is-error': !entry.ok }"
        >
          <header class="entry__head">
            <span class="entry__method" :class="`method-${entry.method}`">{{ entry.method }}</span>
            <span class="entry__endpoint">{{ entry.endpoint }}</span>
            <span class="entry__status" :class="`status-${statusBucket(entry.status)}`">
              {{ entry.status }}
            </span>
          </header>
          <p v-if="!entry.ok && entry.message" class="entry__message">
            {{ entry.message }}<span v-if="entry.code"> ({{ entry.code }})</span>
          </p>
          <footer class="entry__foot">
            <span>{{ entry.elapsed_ms }} ms</span>
            <span>{{ formatTime(entry.time) }}</span>
          </footer>
        </article>
      </section>

      <footer class="api-debug-panel__footer">
        <button type="button" class="api-debug-panel__clear" @click="onClear">
          清空
        </button>
      </footer>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * ApiDebugPanel - API 调试面板
 *
 *  - v-model 控制显示
 *  - 订阅 useApiDebug() 实时刷新最近 10 条请求
 *  - 顶部 4 个状态徽标 (dataDate / demo_mode / stale / partial_success), 缺省显示 "—"
 *  - 底部清空按钮调用 useApiDebug().clear()
 *  - 状态徽标 prop 由宿主页面传入; 等待 T2.x / T3.x 接入真实后端 meta
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { useApiDebug, type ApiDebugEntry } from '../../services/api'

interface Props {
  /** v-model 控制显示 */
  modelValue: boolean
  /** 当前数据日期 (例如 "2026-06-12"), 缺省显示 "—" */
  dataDate?: string
  /** 是否处于 demo / mock 模式 */
  demoMode?: boolean
  /** 数据是否过期 (stale) */
  stale?: boolean
  /** 是否部分字段缺失 (partial_success) */
  partialSuccess?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  dataDate: '',
  demoMode: undefined,
  stale: undefined,
  partialSuccess: undefined,
})
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const { clear, subscribe } = useApiDebug()

const entries = ref<ApiDebugEntry[]>([])
const bufferSize = ref(0)

const unsubscribe = subscribe((next) => {
  entries.value = next
  bufferSize.value = next.length
})

onBeforeUnmount(() => {
  unsubscribe()
})

function close() {
  // 仅在面板打开时发出关闭事件, props.modelValue 用于保持响应式追踪
  if (props.modelValue) {
    emit('update:modelValue', false)
  }
}

function onClear() {
  clear()
}

/**
 * 将可选 boolean 渲染为 "true" / "false" / "—"
 */
function formatBool(value: boolean | undefined): string {
  if (value === undefined || value === null) return '—'
  return value ? 'true' : 'false'
}

/**
 * 三态徽标样式: undefined → 灰色 "—"; true → 绿色 on; false → 灰色 off
 */
function badgeClass(value: boolean | undefined) {
  if (value === undefined || value === null) return 'badge--unknown'
  return value ? 'badge--on' : 'badge--off'
}

const dataDateDisplay = computed(() => props.dataDate && props.dataDate.trim() ? props.dataDate.trim() : '—')
const dataDateBadgeClass = computed(() => props.dataDate && props.dataDate.trim() ? 'badge--on' : 'badge--unknown')

function statusBucket(status: number | string) {
  if (status === 0) return 'network'
  if (typeof status === 'number') {
    if (status >= 500) return 'server'
    if (status >= 400) return 'client'
    if (status >= 200) return 'ok'
  }
  return 'unknown'
}

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}
</script>

<style scoped>
.api-debug-panel {
  position: fixed;
  right: 16px;
  bottom: 16px;
  width: 380px;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border: 1px solid #cbd5e1;
  border-radius: 12px;
  box-shadow: 0 10px 25px rgba(15, 23, 42, 0.15);
  z-index: 9999;
  font-size: 12px;
  color: #0f172a;
}
.api-debug-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid #e2e8f0;
}
.api-debug-panel__close {
  border: 0;
  background: transparent;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  color: #64748b;
}
.api-debug-panel__badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px;
  border-bottom: 1px solid #e2e8f0;
  background: #f8fafc;
}
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}
.badge__label {
  opacity: 0.75;
}
.badge__value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.badge--off { color: #475569; background: #e2e8f0; }
.badge--on { color: #047857; background: #d1fae5; }
.badge--unknown { color: #64748b; background: #e2e8f0; font-style: italic; }
.badge--count { color: #1e40af; background: #dbeafe; }
.api-debug-panel__list {
  flex: 1;
  overflow: auto;
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.api-debug-panel__empty {
  text-align: center;
  color: #94a3b8;
  padding: 20px 0;
}
.api-debug-panel__entry {
  padding: 8px 10px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
}
.api-debug-panel__entry.is-error { border-color: #fecaca; background: #fff7f7; }
.entry__head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.entry__method {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  color: #fff;
}
.method-GET { background: #2563eb; }
.method-POST { background: #047857; }
.method-PUT { background: #d97706; }
.method-DELETE { background: #dc2626; }
.method-PATCH { background: #7c3aed; }
.entry__endpoint { flex: 1; word-break: break-all; }
.entry__status {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  color: #fff;
}
.status-ok { background: #047857; }
.status-client { background: #d97706; }
.status-server { background: #dc2626; }
.status-network { background: #475569; }
.status-unknown { background: #64748b; }
.entry__message {
  margin: 4px 0 0;
  color: #b91c1c;
  font-size: 11px;
  word-break: break-word;
}
.entry__foot {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  color: #64748b;
  font-size: 11px;
}
.api-debug-panel__footer {
  padding: 8px 12px;
  border-top: 1px solid #e2e8f0;
  text-align: right;
}
.api-debug-panel__clear {
  padding: 5px 12px;
  border: 0;
  border-radius: 6px;
  background: #e2e8f0;
  color: #0f172a;
  cursor: pointer;
  font-size: 12px;
}
.api-debug-panel__clear:hover { background: #cbd5e1; }
</style>
