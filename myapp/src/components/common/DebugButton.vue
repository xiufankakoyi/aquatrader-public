<template>
  <div>
    <!-- 吸顶 fixed 按钮, 不破坏宿主页面布局 -->
    <button
      type="button"
      class="debug-button"
      :class="{ 'is-active': debugOpen }"
      :aria-label="debugOpen ? '关闭 API 调试面板' : '打开 API 调试面板'"
      :aria-expanded="debugOpen"
      title="API 诊断 / Debug"
      @click="toggle"
    >
      <span class="debug-button__icon" aria-hidden="true">🛠</span>
      <span class="debug-button__label">诊断</span>
    </button>

    <!-- 内嵌 ApiDebugPanel (v-model 受控, Teleport 到 body) -->
    <ApiDebugPanel
      v-model="debugOpen"
      :data-date="dataDate"
      :demo-mode="demoMode"
      :stale="stale"
      :partial-success="partialSuccess"
    />
  </div>
</template>

<script setup lang="ts">
/**
 * DebugButton - 全局 Debug 面板入口按钮
 *
 * 设计目标:
 *  - 吸顶 fixed 按钮, 浮在屏幕右上角; 不依赖父容器布局, 不破坏现有 <template>
 *  - 内部维护 debugOpen 状态, 嵌入 ApiDebugPanel 复用 v-model 协议
 *  - 状态徽标 (dataDate / demoMode / stale / partialSuccess) 由宿主页面以 prop 传入
 *  - 当前任务 T1.3 中三个 boolean 默认 false, T2.x / T3.x 接入真实后端 meta
 */
import { ref } from 'vue'
import ApiDebugPanel from './ApiDebugPanel.vue'

interface Props {
  /** 当前数据日期, 缺省时 ApiDebugPanel 显示 "—" */
  dataDate?: string
  // TODO(spec browser-runtime-green/T2.x): 从后端 meta 读取 demo_mode
  /** 是否处于 demo / mock 模式 */
  demoMode?: boolean
  // TODO(spec browser-runtime-green/T2.x): 从后端 meta 读取 stale
  /** 数据是否过期 */
  stale?: boolean
  // TODO(spec browser-runtime-green/T2.x): 从后端 meta 读取 partial_success
  /** 是否部分字段缺失 */
  partialSuccess?: boolean
}

withDefaults(defineProps<Props>(), {
  dataDate: '',
  demoMode: undefined,
  stale: undefined,
  partialSuccess: undefined,
})

const debugOpen = ref(false)

/** 切换 ApiDebugPanel 显示 */
function toggle() {
  debugOpen.value = !debugOpen.value
}
</script>

<style scoped>
.debug-button {
  position: fixed;
  top: 12px;
  right: 12px;
  z-index: 9000;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid rgba(148, 163, 184, 0.45);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.85);
  color: #1e293b;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.12);
  transition: transform 0.12s ease, background 0.12s ease, border-color 0.12s ease;
}
.debug-button:hover {
  background: rgba(241, 245, 249, 0.95);
  border-color: rgba(100, 116, 139, 0.6);
}
.debug-button:active {
  transform: scale(0.97);
}
.debug-button.is-active {
  background: #1e293b;
  color: #f8fafc;
  border-color: #1e293b;
}
.debug-button__icon {
  font-size: 13px;
  line-height: 1;
}
.debug-button__label {
  letter-spacing: 0.04em;
}
</style>
