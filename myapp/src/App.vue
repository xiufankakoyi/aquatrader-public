<template>
  <div class="app-container">
    <!-- 加载界面 - 保持显示直到动画完成 -->
    <SplashScreen
      v-if="showSplash"
      @ready="onSplashReady"
      @error="onSplashError"
    />

    <!-- 主界面 - 在加载界面消失前就开始渲染并淡入 -->
    <Transition name="main-fade">
      <div v-show="showMainContent" class="main-content">
        <RouterView />
      </div>
    </Transition>

    <BacktestLoading />
    <ErrorReporter />
    <DataStatusAlert />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import SplashScreen from './components/SplashScreen.vue'
import BacktestLoading from './components/BacktestLoading.vue'
import ErrorReporter from './components/ErrorReporter.vue'
import DataStatusAlert from './components/modals/DataStatusAlert.vue'

const showSplash = ref(true)
const showMainContent = ref(false)

const onSplashReady = () => {
  // 先让主界面开始淡入（此时加载界面还在）
  showMainContent.value = true
  
  // 等待主界面淡入和加载界面退出动画完成后，再移除加载界面
  setTimeout(() => {
    showSplash.value = false
  }, 800)
}

const onSplashError = (code: string, message: string) => {
  console.error('Startup error:', code, message)
}

onMounted(() => {
  const skipSplash = localStorage.getItem('aquatrade_skip_splash') === 'true'
  if (skipSplash) {
    showSplash.value = false
    showMainContent.value = true
  }
})
</script>

<style scoped>
.app-container {
  min-height: 100vh;
  background: #0A0A0A;
}

.main-content {
  min-height: 100vh;
}

.main-fade-enter-active {
  transition: opacity 0.6s ease 0.2s;
}

.main-fade-enter-from {
  opacity: 0;
}

.main-fade-enter-to {
  opacity: 1;
}
</style>
