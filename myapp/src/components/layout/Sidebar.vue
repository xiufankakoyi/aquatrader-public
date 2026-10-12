<template>
  <aside
    class="sidebar"
    :class="{ 'collapsed': isCollapsed }"
  >
    <!-- Logo -->
    <div class="sidebar-header">
      <div v-if="isCollapsed" class="logo-collapsed">
        <i class="fas fa-water logo-icon"></i>
      </div>
      <div v-else class="logo-expanded">
        <span class="logo-text">AquaTrade</span>
        <span class="logo-subtitle">Quant</span>
      </div>
    </div>

    <!-- Toggle Button -->
    <button
      @click="toggleCollapse"
      class="toggle-btn"
      :title="isCollapsed ? '展开侧边栏' : '收起侧边栏'"
    >
      <i class="fas" :class="isCollapsed ? 'fa-chevron-right' : 'fa-chevron-left'"></i>
    </button>

    <!-- Navigation -->
    <nav class="nav-container">
      <div
        v-for="group in navGroups"
        :key="group.key"
        class="nav-group"
        :class="{ 'collapsed': isCollapsed }"
      >
        <!-- Group Title -->
        <div v-if="!isCollapsed" class="group-title">
          <span class="group-label">{{ group.label }}</span>
          <div class="group-divider"></div>
        </div>

        <!-- Group Items -->
        <div class="group-items">
          <router-link
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            class="nav-item"
            :class="{ 'active': item.isActive, 'collapsed': isCollapsed }"
            :title="isCollapsed ? item.label : ''"
          >
            <i class="nav-icon fas" :class="item.icon"></i>
            <span v-if="!isCollapsed" class="nav-label">{{ item.label }}</span>

            <!-- Tooltip for collapsed mode -->
            <div v-if="isCollapsed" class="nav-tooltip">
              {{ item.label }}
              <div class="tooltip-arrow"></div>
            </div>

            <!-- Active indicator (collapsed) -->
            <div v-if="isCollapsed && item.isActive" class="active-indicator"></div>
          </router-link>
        </div>
      </div>
    </nav>
  </aside>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { useRoute } from 'vue-router';
import { useDashboardStore } from '../../store/dashboardStore';

const route = useRoute();
const dashboardStore = useDashboardStore();

const isCollapsed = ref(false);

const isActive = (path: string) => {
  if (path === '/dashboard') return route.path === '/dashboard';
  if (path.startsWith('/strategy')) return route.path.startsWith('/strategy');
  return route.path === path;
};

interface NavItem {
  to: string;
  icon: string;
  label: string;
  isActive: boolean;
}

interface NavGroup {
  key: string;
  label: string;
  items: NavItem[];
}

const navGroups = computed<NavGroup[]>(() => [
  {
    key: 'core',
    label: '核心工作流',
    items: [
      {
        to: '/dashboard',
        icon: 'fa-th-large',
        label: '工作台',
        isActive: isActive('/dashboard'),
      },
      {
        to: '/strategy-center',
        icon: 'fa-code',
        label: '策略中心',
        isActive: isActive('/strategy-center'),
      },
      {
        to: `/strategy/${dashboardStore.selectedStrategyId || 'apex_convergence_v1'}`,
        icon: 'fa-chart-line',
        label: '策略详情',
        isActive: isActive('/strategy'),
      },
      {
        to: '/param-optimization',
        icon: 'fa-sliders-h',
        label: '参数优化',
        isActive: isActive('/param-optimization'),
      },
      {
        to: '/stock-screener',
        icon: 'fa-filter',
        label: '股票筛选器',
        isActive: isActive('/stock-screener'),
      },
    ],
  },
  {
    key: 'research',
    label: '市场研究',
    items: [
      {
        to: '/dragon-eye',
        icon: 'fa-dragon',
        label: 'DragonEye 监控',
        isActive: isActive('/dragon-eye'),
      },
      {
        to: '/stock-sentiment',
        icon: 'fa-comments',
        label: '情绪分析',
        isActive: isActive('/stock-sentiment'),
      },
      {
        to: '/similarity',
        icon: 'fa-shapes',
        label: '相似形态研究',
        isActive: isActive('/similarity'),
      },
      {
        to: '/kline-game',
        icon: 'fa-gamepad',
        label: 'K线盯盘训练',
        isActive: isActive('/kline-game'),
      },
    ],
  },
  {
    key: 'portfolio',
    label: '组合与交易',
    items: [
      {
        to: '/portfolio',
        icon: 'fa-wallet',
        label: '模拟持仓',
        isActive: isActive('/portfolio'),
      },
      {
        to: '/history',
        icon: 'fa-clock',
        label: '交易记录',
        isActive: isActive('/history'),
      },
    ],
  },
  {
    key: 'system',
    label: '系统与数据',
    items: [
      {
        to: '/industry-chain',
        icon: 'fa-network-wired',
        label: '产业链雷达',
        isActive: isActive('/industry-chain'),
      },
      {
        to: '/research/company',
        icon: 'fa-file-lines',
        label: '公司研究档案',
        isActive: isActive('/research/company'),
      },
      {
        to: '/research/workbench',
        icon: 'fa-chalkboard-user',
        label: '机构级研究工作台',
        isActive: isActive('/research/workbench'),
      },
      {
        to: '/finance-radar',
        icon: 'fa-chart-pie',
        label: '财务雷达',
        isActive: isActive('/finance-radar'),
      },
      {
        to: '/knowledge-graph',
        icon: 'fa-project-diagram',
        label: '证据边知识图谱',
        isActive: isActive('/knowledge-graph'),
      },
    ],
  },
]);

const toggleCollapse = () => {
  isCollapsed.value = !isCollapsed.value;
};
</script>

<style scoped>
.sidebar {
  background: var(--bg-primary, #0a0a0a);
  border-right: 1px solid var(--border-color, #1a1a1a);
  display: flex;
  flex-direction: column;
  transition: width 0.3s ease;
  position: relative;
  width: 15rem;
  flex-shrink: 0;
  overflow: visible;
  z-index: 100;
  box-shadow: 4px 0 12px rgba(0, 0, 0, 0.4);
}

.sidebar.collapsed {
  width: 3.5rem;
}

/* Header */
.sidebar-header {
  height: 3rem;
  border-bottom: 1px solid var(--border-color, #1a1a1a);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.logo-collapsed {
  display: flex;
  align-items: center;
  justify-content: center;
}

.logo-icon {
  color: var(--accent-primary, #2962FF);
  font-size: 1.25rem;
}

.logo-expanded {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.logo-text {
  font-size: 1rem;
  font-weight: 600;
  color: #e5e7eb;
  letter-spacing: -0.025em;
}

.logo-subtitle {
  font-size: 0.5625rem;
  color: #525252;
  font-weight: 500;
  letter-spacing: 0.15em;
  text-transform: uppercase;
}

/* Toggle Button */
.toggle-btn {
  position: absolute;
  right: -0.75rem;
  top: 3.5rem;
  width: 1.5rem;
  height: 1.5rem;
  background: var(--bg-secondary, #1f1f1f);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #737373;
  cursor: pointer;
  transition: all 0.2s ease;
  z-index: 10;
  padding: 0;
}

.toggle-btn:hover {
  background: var(--border-hover, #2a2a2a);
  color: #a3a3a3;
}

.toggle-btn i {
  font-size: 0.625rem;
}

/* Navigation */
.nav-container {
  flex: 1;
  padding: 0.5rem 0;
  overflow-y: auto;
  overflow-x: visible;
}

/* Nav Group */
.nav-group {
  margin-bottom: 0.5rem;
}

.nav-group.collapsed {
  margin-bottom: 0.25rem;
}

/* Group Title */
.group-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 1rem 0.25rem;
}

.group-label {
  font-size: 0.6875rem;
  font-weight: 600;
  color: #525252;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  white-space: nowrap;
  flex-shrink: 0;
}

.group-divider {
  flex: 1;
  height: 1px;
  background: var(--border-color, #2a2a2a);
  min-width: 0;
}

/* Group Items */
.group-items {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  padding: 0 0.5rem;
}

/* Nav Item */
.nav-item {
  display: flex;
  align-items: center;
  border-radius: 0.375rem;
  transition: all 0.2s ease;
  text-decoration: none;
  color: #a3a3a3;
  position: relative;
  padding: 0.5rem 0.625rem;
}

.nav-item:hover {
  background: var(--bg-tertiary, #141414);
  color: #d4d4d4;
}

.nav-item.active {
  background: rgba(41, 98, 255, 0.12);
  color: var(--accent-primary, #2962FF);
}

.nav-item.collapsed {
  justify-content: center;
  padding: 0.625rem 0.375rem;
}

.nav-icon {
  font-size: 0.875rem;
  transition: transform 0.2s ease;
  width: 1.25rem;
  text-align: center;
  flex-shrink: 0;
}

.nav-item:hover .nav-icon {
  transform: scale(1.1);
}

.nav-item.active .nav-icon {
  color: var(--accent-primary, #2962FF);
}

.nav-label {
  margin-left: 0.625rem;
  font-size: 0.8125rem;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Tooltip */
.nav-tooltip {
  position: absolute;
  left: calc(100% + 0.5rem);
  top: 50%;
  transform: translateY(-50%);
  padding: 0.375rem 0.625rem;
  background: var(--bg-secondary, #1f1f1f);
  color: #d4d4d4;
  font-size: 0.8125rem;
  font-weight: 500;
  border-radius: 0.375rem;
  white-space: nowrap;
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.2s ease, visibility 0.2s ease;
  z-index: 9999;
  border: 1px solid var(--border-card, #2a2a2a);
  pointer-events: none;
}

.nav-item:hover .nav-tooltip {
  opacity: 1;
  visibility: visible;
}

.tooltip-arrow {
  position: absolute;
  left: -4px;
  top: 50%;
  transform: translateY(-50%);
  width: 0;
  height: 0;
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
  border-right: 4px solid var(--bg-secondary, #1f1f1f);
}

/* Active indicator */
.active-indicator {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 1.25rem;
  background: var(--accent-primary, #2962FF);
  border-radius: 0 2px 2px 0;
}

/* 响应式适配 */
@media (max-width: 991px) {
  .sidebar {
    width: 3.5rem;
  }

  .nav-label,
  .group-title {
    display: none;
  }

  .nav-item {
    justify-content: center;
    padding: 0.625rem 0.375rem;
  }

  .toggle-btn {
    display: none;
  }
}

@media (max-width: 767px) {
  .sidebar {
    width: 0;
    position: fixed;
    z-index: 50;
    overflow-x: hidden;
  }

  .sidebar.open {
    width: 15rem;
  }

  .nav-label,
  .group-title {
    display: block;
  }
}
</style>
