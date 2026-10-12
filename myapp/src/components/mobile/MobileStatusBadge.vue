<template>
  <span class="mobile-status-badge" :class="badgeClass">
    <span v-if="status === 'RUNNING'" class="pulse-dot" />
    {{ label }}
  </span>
</template>

<script setup lang="ts">
/**
 * MobileStatusBadge - 移动端状态徽标组件
 * 根据状态值显示不同颜色的标签，RUNNING 状态带脉冲动画
 */
import { computed } from 'vue';

const props = defineProps<{
  /** 状态值 */
  status: string;
}>();

/** 状态颜色映射 */
const STATUS_MAP: Record<string, { color: string; label: string }> = {
  NEW: { color: 'blue', label: '新发现' },
  WATCHING: { color: 'orange', label: '观察中' },
  DEEP_RESEARCH_READY: { color: 'green', label: '可深研' },
  DISMISSED: { color: 'gray', label: '已忽略' },
  STALE: { color: 'red', label: '已过期' },
  QUEUED: { color: 'blue', label: '排队中' },
  RUNNING: { color: 'orange', label: '运行中' },
  SUCCESS: { color: 'green', label: '成功' },
  FAILED: { color: 'red', label: '失败' },
  PARTIAL: { color: 'yellow', label: '部分完成' },
  CANCELLED: { color: 'gray', label: '已取消' }
};

const badgeClass = computed(() => {
  const color = STATUS_MAP[props.status]?.color || 'gray';
  return `badge-${color}`;
});

const label = computed(() => {
  return STATUS_MAP[props.status]?.label || props.status;
});
</script>

<style scoped>
.mobile-status-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 500;
  border-radius: 4px;
  white-space: nowrap;
}

.badge-blue {
  background: rgba(41, 98, 255, 0.15);
  color: #4c7fff;
}

.badge-orange {
  background: rgba(245, 166, 35, 0.15);
  color: #f5a623;
}

.badge-green {
  background: rgba(8, 153, 129, 0.15);
  color: #0db89a;
}

.badge-red {
  background: rgba(242, 54, 69, 0.15);
  color: #f55763;
}

.badge-yellow {
  background: rgba(255, 193, 7, 0.15);
  color: #ffc107;
}

.badge-gray {
  background: rgba(120, 123, 134, 0.15);
  color: #787b86;
}

/* 脉冲动画 - RUNNING 状态 */
.pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #f5a623;
  animation: pulse-anim 1.5s infinite ease-in-out;
}

@keyframes pulse-anim {
  0%,
  100% {
    opacity: 0.4;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.2);
  }
}
</style>
