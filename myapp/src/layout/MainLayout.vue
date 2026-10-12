<template>
  <div class="main-layout">
    <Sidebar />
    <div class="main-content-wrapper">
      <TopBar />
      <main class="main-content-area" :class="{ 'no-padding': isWorkbench }">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import Sidebar from '../components/layout/Sidebar.vue';
import TopBar from '../components/layout/TopBar.vue';

const route = useRoute();

// 判断是否为工作台页面
const isWorkbench = computed(() => {
  return route.name === 'StrategyEditor';
});
</script>

<style scoped>
.main-layout {
  display: flex;
  min-height: 100vh;
  background: var(--bg-primary, #0A0A0A);
  color: var(--text-primary, #d1d4dc);
}

.main-content-wrapper {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.main-content-area {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  min-height: 0;
  /* 统一内边距，避免内容紧贴Sidebar */
  padding: 12px;
}

.main-content-area.no-padding {
  padding: 0;
  overflow: hidden;
}

/* 响应式适配 */
@media (max-width: 991px) {
  .main-layout {
    flex-direction: row;
  }
}

@media (max-width: 767px) {
  .main-layout {
    flex-direction: column;
  }

  .main-content-wrapper {
    width: 100%;
  }
}
</style>
