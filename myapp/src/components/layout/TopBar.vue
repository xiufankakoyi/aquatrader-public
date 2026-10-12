<template>
  <header class="topbar">
    <!-- 左侧：策略选择 -->
    <div class="topbar-left">
      <select
        v-model="selectedStrategyId"
        @change="handleStrategyChange"
        class="strategy-select"
      >
        <option v-for="strategy in strategies" :key="strategy.id" :value="strategy.id">
          {{ strategy.name }}
        </option>
      </select>

      <!-- 时间周期选择 -->
      <div class="period-selector">
        <button
          v-for="period in quickPeriods"
          :key="period.value"
          @click="setQuickRange(period.value)"
          class="period-btn"
          :class="{ 'active': isActivePeriod(period.value) }"
        >
          {{ period.label }}
        </button>
      </div>

      <!-- 日期范围 -->
      <div class="date-range">
        <input
          v-model="backtestParams.startDate"
          type="date"
          class="date-input"
          @change="handleDateRangeChange"
        />
        <span class="date-separator">-</span>
        <input
          v-model="backtestParams.endDate"
          type="date"
          class="date-input"
          @change="handleDateRangeChange"
        />
      </div>
    </div>

    <!-- 中间：运行状态 -->
    <div v-if="running" class="topbar-center">
      <div class="status-badge">
        <div class="status-dot"></div>
        <span>运行中</span>
      </div>
      <button
        @click="handleStopBacktest"
        class="stop-btn"
      >
        <i class="fas fa-stop"></i>
        <span>停止</span>
      </button>
    </div>

    <!-- 右侧：操作按钮 -->
    <div class="topbar-right">
      <!-- API 状态 -->
      <div class="api-status">
        <div :class="['status-indicator', apiConnected ? 'connected' : 'disconnected']"></div>
        <span class="status-text">{{ apiConnected ? 'API' : '断开' }}</span>
      </div>

      <!-- 数据更新时间 -->
      <div v-if="lastUpdateTime" class="update-time">
        <i class="fas fa-database"></i>
        <span>{{ lastUpdateTime }}</span>
      </div>

      <!-- 预设选择 -->
      <select
        v-if="profiles.length"
        v-model="selectedProfileId"
        class="profile-select"
      >
        <option :value="null">预设</option>
        <option v-for="p in profiles" :key="p.id" :value="String(p.id)">
          {{ p.profile_name }}
        </option>
      </select>

      <!-- 运行回测按钮 -->
      <button
        type="button"
        @click.prevent="handleRunBacktest"
        @mouseenter="handleButtonHover"
        @mouseleave="cancelPreload"
        :disabled="isLoading || !apiConnected || !selectedStrategyId"
        class="run-btn"
      >
        <i v-if="!isLoading" class="fas fa-play"></i>
        <i v-else class="fas fa-spinner fa-spin"></i>
        <span>{{ isLoading ? '运行中' : '回测' }}</span>
      </button>

      <!-- 查询缺失日期按钮 -->
      <button
        type="button"
        @click="showMissingDatesModal"
        class="missing-dates-btn"
        title="查询缺失日期"
      >
        <i class="fas fa-search-minus"></i>
      </button>

      <!-- 数据同步按钮 -->
      <button
        type="button"
        @click="showDataUpdateModal"
        class="sync-btn"
        title="同步市场数据"
      >
        <i class="fas fa-cloud-download-alt"></i>
      </button>

      <!-- 主题切换 -->
      <ThemeSwitcher />
    </div>
  </header>

  <DataUpdateModal ref="dataUpdateModalRef" />
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { useSocketIO } from '../../composables/useSocketIO';
import { useBacktestStore } from '../../store/backtestStore';
import { useDashboardStore } from '../../store/dashboardStore';
import { useHistoryStore } from '../../store/historyStore';
import { useStreamingBacktest } from '../../composables/useStreamingBacktest';
import { fetchStrategyProfiles, preloadBacktest } from '../../api/backtestApi';
import DataUpdateModal from '../modals/DataUpdateModal.vue';
import ThemeSwitcher from '../ThemeSwitcher.vue';

const dataUpdateModalRef = ref<any>(null);

const showDataUpdateModal = () => {
    dataUpdateModalRef.value?.show();
};

const showMissingDatesModal = () => {
    dataUpdateModalRef.value?.queryMissingDates();
};

const { connect } = useSocketIO();
const router = useRouter();
const route = useRoute();
const backtestStore = useBacktestStore();
const dashboardStore = useDashboardStore();
const historyStore = useHistoryStore();
const { start: startBacktest, isRunning, cancel: cancelBacktest } = useStreamingBacktest();

const hoverTimer = ref<number | null>(null);
const isPreloading = ref(false);
const preloadTaskId = ref<string | null>(null);
const activePeriod = ref<string>('1y');

const quickPeriods = [
  { label: '1月', value: '1m' },
  { label: '3月', value: '3m' },
  { label: '6月', value: '6m' },
  { label: '1年', value: '1y' },
  { label: '3年', value: '3y' },
  { label: 'YTD', value: 'ytd' },
];

const isActivePeriod = (value: string) => activePeriod.value === value;

const isLoading = computed(() => backtestStore.running || isRunning.value);
const running = computed(() => backtestStore.running || isRunning.value);
const lastUpdateTime = computed(() => backtestStore.lastUpdated);
const strategies = computed(() => dashboardStore.strategies);
const selectedStrategyId = computed({
  get: () => dashboardStore.selectedStrategyId,
  set: (value) => dashboardStore.setSelectedStrategy(value || '')
});
const apiConnected = computed(() => dashboardStore.apiConnected);

// 策略参数预设（Profile）相关状态
const profiles = ref<any[]>([]);
const selectedProfileId = ref<string | null>(null);

// 回测参数
const backtestParams = ref({
  startDate: '2024-01-01',
  endDate: '2024-03-31',
  benchmarkCode: '000300'
});

const formatDate = (d: Date): string => d.toISOString().split('T')[0] || '';

function setQuickRange(type: string) {
  activePeriod.value = type;
  const end = new Date();
  let start = new Date(end);

  switch (type) {
    case '1m':
      start.setMonth(end.getMonth() - 1);
      break;
    case '3m':
      start.setMonth(end.getMonth() - 3);
      break;
    case '6m':
      start.setMonth(end.getMonth() - 6);
      break;
    case '1y':
      start.setFullYear(end.getFullYear() - 1);
      break;
    case '3y':
      start.setFullYear(end.getFullYear() - 3);
      break;
    case 'ytd':
      start = new Date(end.getFullYear(), 0, 1);
      break;
  }

  backtestParams.value.startDate = formatDate(start);
  backtestParams.value.endDate = formatDate(end);
  handleDateRangeChange();
}

function handleStrategyChange() {
  dashboardStore.setSelectedStrategy(selectedStrategyId.value || '');

  // 如果当前在策略详情页，切换策略时需要更新路由
  if (route.name === 'StrategyDetail') {
    router.push({ name: 'StrategyDetail', params: { id: selectedStrategyId.value } });
  }

  // 策略变化时刷新该策略下的预设列表
  const strategy = dashboardStore.selectedStrategy;
  profiles.value = [];
  selectedProfileId.value = null;
  if (strategy) {
    fetchStrategyProfiles(strategy.name)
      .then((list) => {
        profiles.value = Array.isArray(list) ? list : [];
      })
      .catch((err) => {
        console.error('获取策略预设失败', err);
      });
  }
}

function handleDateRangeChange() {
  // 日期区间变化时，可以触发一些操作，比如更新回测参数
  // 这里暂时不做任何操作，只是保存到 backtestParams
}

async function handleRunBacktest() {
  if (!selectedStrategyId.value || isLoading.value || !apiConnected.value) {
    return;
  }

  const strategy = dashboardStore.selectedStrategy;
  if (!strategy) return;

  try {
    startBacktest(
      {
        strategy_name: strategy.name,
        start_date: backtestParams.value.startDate,
        end_date: backtestParams.value.endDate,
        benchmark_code: backtestParams.value.benchmarkCode || null,
        profile_id: selectedProfileId.value ? Number(selectedProfileId.value) : undefined,
      },
      {
        onError: (error) => {
          dashboardStore.error = error.message;
        },
        onCancel: () => {
          console.log('回测已取消');
        },
      }
    );
  } catch (error) {
    dashboardStore.error = error instanceof Error ? error.message : '启动回测失败';
  }
}

function handleStopBacktest() {
  if (!running.value) return;
  cancelBacktest();
}

function handleButtonHover() {
  if (isLoading.value || !apiConnected.value || !selectedStrategyId.value) {
    return;
  }

  hoverTimer.value = window.setTimeout(() => {
    triggerPreload();
  }, 200);
}

function cancelPreload() {
  if (hoverTimer.value) {
    clearTimeout(hoverTimer.value);
    hoverTimer.value = null;
  }
}

async function triggerPreload() {
  if (isPreloading.value || !selectedStrategyId.value) {
    return;
  }

  const strategy = dashboardStore.selectedStrategy;
  if (!strategy) return;

  isPreloading.value = true;

  try {
    const response = await preloadBacktest(
      strategy.name,
      backtestParams.value.startDate,
      backtestParams.value.endDate
    );

    if (response.success) {
      preloadTaskId.value = response.task_id;
      console.log('[Preload] 预加载任务已启动:', response.task_id);
    }
  } catch (error) {
    console.warn('[Preload] 预加载失败:', error);
  } finally {
    isPreloading.value = false;
  }
}

onMounted(async () => {
  // 使用相对路径 ''，让 Socket.IO 自动使用当前域名
  // 这样 Vite 代理可以正确代理 /socket.io 请求到后端
  connect('');
  await dashboardStore.loadStrategies();
  historyStore.loadFromStorage();
});

onUnmounted(() => {
  cancelPreload();
});
</script>

<style scoped>
.topbar {
  height: clamp(2.5rem, 8vh, 2.75rem);
  background: var(--bg-primary, #0a0a0a);
  border-bottom: 1px solid var(--border-color, #1a1a1a);
  display: flex;
  align-items: center;
  padding: 0 clamp(0.5rem, 1vw, 0.75rem);
  gap: clamp(0.5rem, 1vw, 1rem);
  flex-wrap: nowrap;
}

/* 左侧区域 */
.topbar-left {
  display: flex;
  align-items: center;
  gap: clamp(0.375rem, 0.75vw, 0.5rem);
  flex: 1;
  min-width: 0;
  flex-wrap: wrap;
}

.strategy-select {
  height: clamp(1.625rem, 5vh, 1.75rem);
  padding: 0 clamp(0.375rem, 0.75vw, 0.5rem);
  background: var(--bg-tertiary, #141414);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 0.25rem;
  font-size: clamp(0.6875rem, 0.8vw, 0.75rem);
  color: #e5e7eb;
  outline: none;
  min-width: clamp(6rem, 10vw, 8rem);
  max-width: 100%;
}

.strategy-select:focus {
  border-color: var(--border-hover, #404040);
}

.period-selector {
  display: flex;
  align-items: center;
  background: var(--bg-tertiary, #141414);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 0.25rem;
  height: clamp(1.625rem, 5vh, 1.75rem);
  flex-shrink: 0;
}

.period-btn {
  padding: 0 clamp(0.375rem, 0.75vw, 0.5rem);
  height: 100%;
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: #a3a3a3;
  background: transparent;
  border: none;
  border-right: 1px solid var(--border-card, #2a2a2a);
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
}

.period-btn:last-child {
  border-right: none;
}

.period-btn:hover {
  color: #e5e7eb;
}

.period-btn.active {
  color: #f5f5f5;
  background: var(--bg-secondary, #1f1f1f);
}

.date-range {
  display: flex;
  align-items: center;
  gap: clamp(0.125rem, 0.25vw, 0.25rem);
  flex-shrink: 0;
}

.date-input {
  height: clamp(1.625rem, 5vh, 1.75rem);
  width: clamp(5.5rem, 9vw, 6.25rem);
  padding: 0 clamp(0.25rem, 0.5vw, 0.375rem);
  background: var(--bg-tertiary, #141414);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 0.25rem;
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: #d4d4d4;
  outline: none;
}

.date-input:focus {
  border-color: var(--border-hover, #404040);
}

.date-separator {
  color: #525252;
  font-size: clamp(0.75rem, 0.9vw, 0.875rem);
}

/* 中间区域 */
.topbar-center {
  display: flex;
  align-items: center;
  gap: clamp(0.375rem, 0.75vw, 0.5rem);
  flex-shrink: 0;
}

.status-badge {
  display: flex;
  align-items: center;
  gap: clamp(0.25rem, 0.5vw, 0.375rem);
  padding: clamp(0.125rem, 0.4vh, 0.25rem) clamp(0.375rem, 0.75vw, 0.5rem);
  background: var(--success-bg, #0d2818);
  border: 1px solid var(--success-border, #1a4d2e);
  border-radius: 0.25rem;
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: var(--success-color, #22c55e);
}

.status-dot {
  width: clamp(0.375rem, 0.5vw, 0.5rem);
  height: clamp(0.375rem, 0.5vw, 0.5rem);
  background: var(--success-color, #22c55e);
  border-radius: 50%;
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.stop-btn {
  height: clamp(1.5rem, 4.5vh, 1.625rem);
  padding: 0 clamp(0.375rem, 0.75vw, 0.5rem);
  background: var(--error-bg, #2a0a0a);
  border: 1px solid var(--error-border, #4a1a1a);
  border-radius: 0.25rem;
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: var(--error-color, #ef4444);
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  gap: clamp(0.25rem, 0.4vw, 0.375rem);
  flex-shrink: 0;
}

.stop-btn:hover {
  background: #3a0f0f;
}

.stop-btn i {
  font-size: clamp(0.5625rem, 0.65vw, 0.625rem);
}

/* 右侧区域 */
.topbar-right {
  display: flex;
  align-items: center;
  gap: clamp(0.375rem, 0.75vw, 0.5rem);
  margin-left: auto;
  flex-shrink: 0;
}

.api-status {
  display: flex;
  align-items: center;
  gap: clamp(0.25rem, 0.4vw, 0.375rem);
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: #737373;
}

.status-indicator {
  width: clamp(0.375rem, 0.5vw, 0.5rem);
  height: clamp(0.375rem, 0.5vw, 0.5rem);
  border-radius: 50%;
}

.status-indicator.connected {
  background: var(--success-color, #22c55e);
}

.status-indicator.disconnected {
  background: var(--error-color, #ef4444);
}

.status-text {
  display: none;
}

@media (min-width: 768px) {
  .status-text {
    display: inline;
  }
}

.update-time {
  display: none;
  align-items: center;
  gap: clamp(0.25rem, 0.4vw, 0.375rem);
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: #737373;
}

@media (min-width: 768px) {
  .update-time {
    display: flex;
  }
}

.update-time i {
  font-size: clamp(0.5625rem, 0.65vw, 0.625rem);
}

.profile-select {
  display: none;
  height: clamp(1.625rem, 5vh, 1.75rem);
  padding: 0 clamp(0.375rem, 0.75vw, 0.5rem);
  background: var(--bg-tertiary, #141414);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 0.25rem;
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  color: #a3a3a3;
  outline: none;
}

@media (min-width: 1024px) {
  .profile-select {
    display: block;
  }
}

.profile-select:focus {
  border-color: var(--border-hover, #404040);
}

.run-btn {
  height: clamp(1.625rem, 5vh, 1.75rem);
  padding: 0 clamp(0.5rem, 1vw, 0.75rem);
  background: var(--accent-primary, #2962FF);
  border: none;
  border-radius: 0.25rem;
  font-size: clamp(0.625rem, 0.7vw, 0.6875rem);
  font-weight: 500;
  color: white;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  gap: clamp(0.25rem, 0.5vw, 0.375rem);
  flex-shrink: 0;
}

.run-btn:hover:not(:disabled) {
  background: var(--accent-secondary, #1e4fd8);
}

.run-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.run-btn i {
  font-size: clamp(0.5625rem, 0.65vw, 0.625rem);
}

.sync-btn {
  width: clamp(1.625rem, 5vh, 1.75rem);
  height: clamp(1.625rem, 5vh, 1.75rem);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-tertiary, #141414);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 0.25rem;
  color: #737373;
  cursor: pointer;
  transition: all 0.15s ease;
  flex-shrink: 0;
}

.sync-btn:hover {
  color: #d4d4d4;
  border-color: var(--border-hover, #404040);
}

.sync-btn i {
  font-size: clamp(0.6875rem, 0.8vw, 0.75rem);
}

.missing-dates-btn {
  width: clamp(1.625rem, 5vh, 1.75rem);
  height: clamp(1.625rem, 5vh, 1.75rem);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-tertiary, #141414);
  border: 1px solid var(--border-card, #2a2a2a);
  border-radius: 0.25rem;
  color: #737373;
  cursor: pointer;
  transition: all 0.15s ease;
  flex-shrink: 0;
}

.missing-dates-btn:hover {
  color: #d4d4d4;
  border-color: var(--border-hover, #404040);
}

.missing-dates-btn i {
  font-size: clamp(0.6875rem, 0.8vw, 0.75rem);
}

/* 响应式适配 */
@media (max-width: 1199px) {
  .topbar {
    padding: 0 clamp(0.375rem, 0.8vw, 0.5rem);
    gap: clamp(0.375rem, 0.8vw, 0.5rem);
  }

  .topbar-left {
    gap: clamp(0.25rem, 0.5vw, 0.375rem);
  }
}

@media (max-width: 991px) {
  .topbar {
    flex-wrap: wrap;
    height: auto;
    min-height: clamp(2.25rem, 7vh, 2.5rem);
    padding: clamp(0.25rem, 0.5vh, 0.375rem) clamp(0.375rem, 0.8vw, 0.5rem);
  }

  .topbar-left {
    width: 100%;
    order: 1;
  }

  .topbar-center,
  .topbar-right {
    order: 2;
  }

  .date-range {
    margin-left: auto;
  }
}

@media (max-width: 767px) {
  .period-selector {
    display: none;
  }

  .strategy-select {
    flex: 1;
    min-width: 0;
  }
}

@media (max-width: 575px) {
  .topbar-left {
    flex-wrap: nowrap;
  }

  .date-input {
    width: clamp(4.5rem, 20vw, 5.5rem);
  }
}
</style>
