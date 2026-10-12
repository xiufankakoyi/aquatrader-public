<!--
  回测加载动画组件
  只在预加载（初始化数据库连接）时显示
-->
<template>
  <div v-if="show" class="backtest-loading-overlay fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center">
    <div class="backtest-loading-content text-center">
      <!-- 旋转的加载圆圈 -->
      <div class="loading-spinner mb-6">
        <div class="spinner-ring"></div>
        <div class="spinner-ring"></div>
        <div class="spinner-ring"></div>
      </div>
      
      <!-- 加载文本 -->
      <h3 class="text-xl font-semibold text-white mb-2">Running Backtest Simulation...</h3>
      <!-- CHANGED: 灰色小字部分，带切换动画 -->
      <p class="text-slate-400 text-sm loading-message" :class="{ 'fade-out': !isMessageVisible }">
        {{ currentMessage }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue';
import { useBacktestStore } from '../store/backtestStore';

const backtestStore = useBacktestStore();

// CHANGED: 只在初始化时显示
const show = computed(() => backtestStore.isInitializing);

// CHANGED: 切换文字的动画
const messages = [
  'Initializing database connection...',
  'Loading market data...',
  'Preparing strategy engine...',
  'Setting up backtest environment...'
];

const currentMessageIndex = ref(0);
const currentMessage = computed(() => messages[currentMessageIndex.value]);
const isMessageVisible = ref(true);

let messageInterval: number | null = null;

// CHANGED: 监听初始化状态，只在初始化时启动文字切换
watch(() => backtestStore.isInitializing, (isInitializing) => {
  if (isInitializing) {
    // 开始切换文字
    messageInterval = window.setInterval(() => {
      isMessageVisible.value = false;
      setTimeout(() => {
        currentMessageIndex.value = (currentMessageIndex.value + 1) % messages.length;
        isMessageVisible.value = true;
      }, 300); // 淡出后切换文字
    }, 2000);
  } else {
    // 停止切换文字
    if (messageInterval !== null) {
      clearInterval(messageInterval);
      messageInterval = null;
    }
  }
});

onUnmounted(() => {
  if (messageInterval !== null) {
    clearInterval(messageInterval);
  }
});
</script>

<style scoped>
.backtest-loading-overlay {
  animation: fadeIn 0.3s ease-in;
}

@keyframes fadeIn {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

.loading-spinner {
  position: relative;
  width: 80px;
  height: 80px;
  margin: 0 auto;
}

.spinner-ring {
  position: absolute;
  width: 100%;
  height: 100%;
  border: 4px solid transparent;
  border-top-color: #6366f1;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

.spinner-ring:nth-child(2) {
  width: 70%;
  height: 70%;
  top: 15%;
  left: 15%;
  border-top-color: #8b5cf6;
  animation-duration: 1.5s;
  animation-direction: reverse;
}

.spinner-ring:nth-child(3) {
  width: 50%;
  height: 50%;
  top: 25%;
  left: 25%;
  border-top-color: #a855f7;
  animation-duration: 2s;
}

@keyframes spin {
  0% {
    transform: rotate(0deg);
  }
  100% {
    transform: rotate(360deg);
  }
}

.backtest-loading-content {
  animation: slideUp 0.3s ease-out;
}

@keyframes slideUp {
  from {
    transform: translateY(20px);
    opacity: 0;
  }
  to {
    transform: translateY(0);
    opacity: 1;
  }
}

/* CHANGED: 文字切换动画 */
.loading-message {
  min-height: 1.5rem;
  transition: opacity 0.3s ease-in-out;
}

.loading-message.fade-out {
  opacity: 0;
}
</style>

