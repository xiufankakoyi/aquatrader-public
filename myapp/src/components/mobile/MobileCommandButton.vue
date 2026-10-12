<template>
  <div class="mobile-command-btn">
    <button
      class="cmd-btn"
      :class="[
        `cmd-btn-${type}`,
        { 'cmd-btn-loading': loading, 'cmd-btn-disabled': disabled }
      ]"
      :disabled="disabled || loading"
      @click="handleClick"
    >
      <i v-if="icon" :class="icon" class="cmd-btn-icon" />
      <span class="cmd-btn-label">{{ label }}</span>
      <a-spin v-if="loading" size="small" class="cmd-btn-spin" />
    </button>

    <!-- 确认弹窗 -->
    <a-modal
      v-model:open="showConfirm"
      :title="confirmTitle"
      :ok-text="'确认执行'"
      :cancel-text="'取消'"
      centered
      :width="320"
      @ok="handleConfirm"
      @cancel="showConfirm = false"
    >
      <p class="confirm-desc">{{ confirmDescription }}</p>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileCommandButton - 移动端命令按钮组件
 * 支持读写类型区分，写命令需二次确认
 */
import { ref } from 'vue';

const props = withDefaults(
  defineProps<{
    /** 按钮标签 */
    label: string;
    /** 图标 class (FontAwesome) */
    icon?: string;
    /** 按钮类型: read=只读命令, write=写命令(需确认) */
    type?: 'read' | 'write';
    /** 是否加载中 */
    loading?: boolean;
    /** 是否禁用 */
    disabled?: boolean;
    /** 确认弹窗标题 */
    confirmTitle?: string;
    /** 确认弹窗描述 */
    confirmDescription?: string;
  }>(),
  {
    icon: '',
    type: 'read',
    loading: false,
    disabled: false,
    confirmTitle: '确认执行',
    confirmDescription: '确认要执行此操作吗？'
  }
);

const emit = defineEmits<{
  /** 执行命令 */
  (e: 'execute'): void;
  /** 确认执行（写命令） */
  (e: 'confirm'): void;
}>();

const showConfirm = ref(false);

function handleClick() {
  if (props.type === 'write') {
    showConfirm.value = true;
  } else {
    emit('execute');
  }
}

function handleConfirm() {
  showConfirm.value = false;
  emit('confirm');
}
</script>

<style scoped>
.cmd-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  padding: 12px 16px;
  font-size: 14px;
  font-weight: 500;
  border-radius: 10px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all var(--transition-fast);
  position: relative;
}

.cmd-btn:active {
  transform: scale(0.97);
}

.cmd-btn-read {
  background: rgba(41, 98, 255, 0.12);
  color: #4c7fff;
  border-color: rgba(41, 98, 255, 0.2);
}

.cmd-btn-read:active {
  background: rgba(41, 98, 255, 0.2);
}

.cmd-btn-write {
  background: rgba(245, 166, 35, 0.12);
  color: #f5a623;
  border-color: rgba(245, 166, 35, 0.2);
}

.cmd-btn-write:active {
  background: rgba(245, 166, 35, 0.2);
}

.cmd-btn-loading,
.cmd-btn-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.cmd-btn-icon {
  font-size: 14px;
}

.cmd-btn-label {
  flex: 1;
  text-align: center;
}

.cmd-btn-spin {
  position: absolute;
  right: 12px;
}

.confirm-desc {
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.6;
}
</style>
