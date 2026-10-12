<template>
  <div class="mobile-layout">
    <!-- 顶部标题栏 -->
    <header class="mobile-header">
      <div class="mobile-header-left">
        <span class="mobile-header-logo">AquaTrade</span>
        <span class="mobile-header-sub">投研</span>
      </div>
      <button class="mobile-header-logout" @click="handleLogout">
        <i class="fa-solid fa-right-from-bracket" />
        <span>登出</span>
      </button>
    </header>

    <!-- 内容区域 -->
    <main class="mobile-content" :class="{ 'no-padding': isFullPage }">
      <RouterView />
    </main>

    <!-- 底部导航栏 -->
    <nav class="mobile-tabbar">
      <div class="tabbar-indicator" :style="indicatorStyle" />
      <div
        v-for="(tab, idx) in tabs"
        :key="tab.path"
        ref="tabRefs"
        class="tabbar-item"
        :class="{ 'tabbar-active': isActive(tab.path) }"
        @click="navigateTo(tab.path)"
      >
        <div class="tabbar-icon-wrap">
          <i :class="tab.icon" class="tabbar-icon" />
        </div>
        <span class="tabbar-label">{{ tab.label }}</span>
      </div>
    </nav>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileLayout - 移动端布局
 * 顶部标题栏 + 中间内容区 + 底部标签导航
 * 包含认证检查，未登录时重定向到登录页
 */
import { onMounted, ref, computed, nextTick, watch } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { getAuthStatus, logout } from '../api/mobile';

const router = useRouter();
const route = useRoute();

/**
 * 全屏页面（策略工作台、回测看板）不需要内容内边距
 * 让编辑器和图表能够铺满整个宽度
 */
const isFullPage = computed(() => {
  return route.path === '/mobile/strategy' || route.path === '/mobile/dashboard';
});

/** 底部导航标签配置 */
const tabs = [
  { path: '/mobile', label: '首页', icon: 'fa-solid fa-house' },
  { path: '/mobile/daily', label: '日报', icon: 'fa-solid fa-newspaper' },
  { path: '/mobile/strategy', label: '策略', icon: 'fa-solid fa-code' },
  { path: '/mobile/dashboard', label: '回测', icon: 'fa-solid fa-chart-line' },
  { path: '/mobile/watchlist', label: '观察池', icon: 'fa-solid fa-eye' },
  { path: '/mobile/chain', label: '产业链', icon: 'fa-solid fa-diagram-project' }
];

const tabRefs = ref<HTMLElement[]>([]);
const indicatorStyle = ref({ left: '0px', width: '0px', opacity: 0 });

const activeIndex = computed(() => {
  const idx = tabs.findIndex((tab) => isActive(tab.path));
  return idx >= 0 ? idx : 0;
});

/** 判断当前标签是否激活 */
function isActive(path: string): boolean {
  if (path === '/mobile') {
    return route.path === '/mobile';
  }
  return route.path.startsWith(path);
}

/** 更新指示器位置 */
function updateIndicator() {
  nextTick(() => {
    const el = tabRefs.value[activeIndex.value];
    if (!el) return;
    const parent = el.parentElement;
    if (!parent) return;
    const parentRect = parent.getBoundingClientRect();
    const tabRect = el.getBoundingClientRect();
    indicatorStyle.value = {
      left: `${tabRect.left - parentRect.left + tabRect.width * 0.2}px`,
      width: `${tabRect.width * 0.6}px`,
      opacity: 1
    };
  });
}

watch(activeIndex, updateIndicator, { flush: 'post' });

/** 导航到指定标签页 */
function navigateTo(path: string) {
  router.push(path);
}

/** 登出 */
async function handleLogout() {
  try {
    await logout();
  } catch {
    // 即使 API 调用失败也继续清理本地状态
  }
  localStorage.removeItem('mobile_auth_token');
  router.replace('/mobile/login');
}

/** 认证检查 */
async function checkAuth() {
  const token = localStorage.getItem('mobile_auth_token');
  if (!token) {
    router.replace('/mobile/login');
    return;
  }
  try {
    await getAuthStatus();
  } catch {
    router.replace('/mobile/login');
  }
}

onMounted(() => {
  checkAuth();
  updateIndicator();
  window.addEventListener('resize', updateIndicator);
});
</script>

<style scoped>
.mobile-layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  min-height: 100dvh;
  max-width: 100vw;
  overflow-x: hidden;
  background: #0a0a0a;
  color: var(--text-primary);
}

/* 顶部标题栏 */
.mobile-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  height: 48px;
  background: rgba(17, 17, 17, 0.95);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  flex-shrink: 0;
  position: sticky;
  top: 0;
  z-index: 100;
}

.mobile-header-left {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.mobile-header-logo {
  font-size: 17px;
  font-weight: 700;
  color: #4c7fff;
  letter-spacing: -0.02em;
}

.mobile-header-sub {
  font-size: 13px;
  color: var(--text-muted);
}

.mobile-header-logout {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-muted);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 6px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.mobile-header-logout:active {
  background: rgba(255, 255, 255, 0.08);
}

/* 内容区域 */
.mobile-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 12px;
  /* 为底部导航留出充足空间（70px 含 border + 安全区域） */
  padding-bottom: calc(16px + 70px + env(safe-area-inset-bottom, 0px));
  box-sizing: border-box;
}

/* 策略工作台和回测看板需要全屏，移除内边距 */
.mobile-content.no-padding {
  padding: 0;
  overflow: hidden;
}

/* 页面底部占位，确保最后一个元素不被固定导航栏遮挡 */
.mobile-bottom-spacer {
  height: calc(70px + env(safe-area-inset-bottom, 0px));
  flex-shrink: 0;
}

/* 底部导航栏 */
.mobile-tabbar {
  display: flex;
  align-items: center;
  justify-content: space-around;
  height: 60px;
  background: rgba(17, 17, 17, 0.98);
  backdrop-filter: blur(16px);
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  flex-shrink: 0;
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 100;
  padding-bottom: env(safe-area-inset-bottom, 0px);
}

.tabbar-indicator {
  position: absolute;
  top: 0;
  height: 2px;
  border-radius: 0 0 2px 2px;
  background: linear-gradient(90deg, var(--accent-primary), var(--accent-primary-light));
  box-shadow: 0 0 10px rgba(41, 98, 255, 0.4);
  transition: left 0.35s cubic-bezier(0.22, 1, 0.36, 1), width 0.35s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.2s ease;
}

.tabbar-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  flex: 1;
  min-width: 0;
  height: 100%;
  cursor: pointer;
  transition: all var(--transition-fast);
  -webkit-tap-highlight-color: transparent;
  padding: 0 2px;
}

.tabbar-item:active .tabbar-icon-wrap {
  transform: scale(0.88);
}

.tabbar-icon-wrap {
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  transition: all 0.25s cubic-bezier(0.22, 1, 0.36, 1);
}

.tabbar-icon {
  font-size: 16px;
  color: var(--text-muted);
  transition: color 0.25s ease;
}

.tabbar-label {
  font-size: 9px;
  color: var(--text-muted);
  transition: color 0.25s ease, font-weight 0.25s ease;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.tabbar-active .tabbar-icon-wrap {
  background: rgba(41, 98, 255, 0.12);
}

.tabbar-active .tabbar-icon {
  color: #4c7fff;
}

.tabbar-active .tabbar-label {
  color: #4c7fff;
  font-weight: 500;
}

@media (prefers-reduced-motion: reduce) {
  .tabbar-indicator {
    transition: none;
  }
}
</style>