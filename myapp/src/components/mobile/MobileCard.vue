<template>
  <div
    class="mobile-card"
    :class="[
      `mobile-card--${accent}`,
      { 'mobile-card-clickable': clickable, 'mobile-card-entrance': animate }
    ]"
    :style="entranceStyle"
    @click="handleClick"
  >
    <div v-if="title" class="mobile-card-header">
      <div class="mobile-card-title-wrap">
        <i v-if="icon" :class="icon" class="mobile-card-icon" />
        <span class="mobile-card-title">{{ title }}</span>
      </div>
      <span v-if="extra && !$slots.extra" class="mobile-card-extra">{{ extra }}</span>
      <span v-if="$slots.extra" class="mobile-card-extra">
        <slot name="extra" />
      </span>
    </div>
    <div class="mobile-card-body">
      <slot />
    </div>
    <div v-if="$slots.footer" class="mobile-card-footer">
      <slot name="footer" />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileCard - 移动端通用卡片组件
 * 深色主题，圆角，微妙边框，支持点击事件、强调色、图标和入场动画
 */

import { computed } from 'vue';

export type CardAccent = 'default' | 'blue' | 'purple' | 'gold' | 'green' | 'red';

const props = withDefaults(
  defineProps<{
    /** 卡片标题 */
    title?: string;
    /** 标题右侧附加内容 */
    extra?: string;
    /** 是否可点击 */
    clickable?: boolean;
    /** 强调色风格 */
    accent?: CardAccent;
    /** 标题左侧图标 class */
    icon?: string;
    /** 是否启用入场动画 */
    animate?: boolean;
    /** 入场动画延迟（毫秒） */
    delay?: number;
  }>(),
  {
    title: '',
    extra: '',
    clickable: false,
    accent: 'default',
    icon: '',
    animate: true,
    delay: 0
  }
);

const emit = defineEmits<{
  /** 点击事件 */
  (e: 'click'): void;
}>();

const entranceStyle = computed(() => ({
  animationDelay: `${props.delay}ms`
}));

function handleClick() {
  emit('click');
}
</script>

<style scoped>
.mobile-card {
  background: linear-gradient(
    145deg,
    rgba(30, 30, 46, 0.9) 0%,
    rgba(26, 26, 46, 0.95) 100%
  );
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  padding: 14px;
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
  position: relative;
  overflow: hidden;
}

.mobile-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.08) 50%,
    transparent 100%
  );
}

.mobile-card::after {
  content: '';
  position: absolute;
  left: 0;
  top: 12px;
  bottom: 12px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: transparent;
  transition: background 0.2s ease;
}

/* 强调色 */
.mobile-card--blue::after {
  background: linear-gradient(180deg, var(--accent-primary-light), var(--accent-primary));
  box-shadow: 0 0 10px rgba(76, 127, 255, 0.25);
}

.mobile-card--purple::after {
  background: linear-gradient(180deg, #a855f7, #7c3aed);
  box-shadow: 0 0 10px rgba(124, 58, 237, 0.25);
}

.mobile-card--gold::after {
  background: linear-gradient(180deg, #f5a623, #d97706);
  box-shadow: 0 0 10px rgba(245, 166, 35, 0.2);
}

.mobile-card--green::after {
  background: linear-gradient(180deg, var(--color-down-light), var(--color-down));
  box-shadow: 0 0 10px rgba(8, 153, 129, 0.2);
}

.mobile-card--red::after {
  background: linear-gradient(180deg, var(--color-up-light), var(--color-up));
  box-shadow: 0 0 10px rgba(242, 54, 69, 0.2);
}

.mobile-card-clickable {
  cursor: pointer;
}

.mobile-card-clickable:active {
  transform: scale(0.98);
}

.mobile-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.mobile-card-title-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mobile-card-icon {
  font-size: 13px;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--text-secondary);
}

.mobile-card--blue .mobile-card-icon {
  color: var(--accent-primary-light);
  background: rgba(41, 98, 255, 0.12);
}

.mobile-card--purple .mobile-card-icon {
  color: #c084fc;
  background: rgba(124, 58, 237, 0.12);
}

.mobile-card--gold .mobile-card-icon {
  color: #fbbf24;
  background: rgba(245, 166, 35, 0.12);
}

.mobile-card--green .mobile-card-icon {
  color: var(--color-down-light);
  background: rgba(8, 153, 129, 0.12);
}

.mobile-card--red .mobile-card-icon {
  color: var(--color-up-light);
  background: rgba(242, 54, 69, 0.12);
}

.mobile-card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}

.mobile-card-extra {
  font-size: 11px;
  color: var(--text-muted);
}

.mobile-card-body {
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.mobile-card-footer {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.04);
}

/* 入场动画 */
@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(16px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.mobile-card-entrance {
  animation: card-enter 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
}

@media (prefers-reduced-motion: reduce) {
  .mobile-card-entrance {
    animation: none;
  }
}
</style>
