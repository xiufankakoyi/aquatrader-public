<template>
  <span class="badge" :class="normalized">{{ label || labels[normalized] }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ status?: string; label?: string }>(), {
  status: 'unknown',
  label: '',
})
const labels = {
  ok: '数据正常',
  warning: '数据警告',
  error: '数据异常',
  unknown: '状态未知',
}
const normalized = computed<keyof typeof labels>(() => {
  const value = props.status.toLowerCase()
  if (value === 'ok') return 'ok'
  if (value === 'warning') return 'warning'
  if (value === 'error' || value === 'critical') return 'error'
  return 'unknown'
})
</script>

<style scoped>
.badge { display: inline-flex; align-items: center; padding: 4px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.ok { color: #047857; background: #d1fae5; }
.warning { color: #92400e; background: #fef3c7; }
.error { color: #b91c1c; background: #fee2e2; }
.unknown { color: #475569; background: #e2e8f0; }
</style>
