<template>
  <div class="workbench-toolbar">
    <!-- 左侧：策略信息 -->
    <div class="toolbar-section left">
      <div class="breadcrumb">
        <router-link to="/dashboard" class="breadcrumb-link">
          <i class="fas fa-chevron-left"></i>
          <span>Dashboard</span>
        </router-link>
        <span class="breadcrumb-separator">/</span>
        <span class="breadcrumb-current">策略工作台</span>
      </div>
      
      <div class="strategy-info">
        <i class="fas fa-code strategy-icon"></i>
        <input
          v-model="localStrategyName"
          type="text"
          class="strategy-name-input"
          placeholder="输入策略名称"
          @blur="updateStrategyName"
        />
        <span v-if="hasUnsavedChanges" class="unsaved-indicator">●</span>
      </div>
    </div>

    <!-- 中间：运行状态 -->
    <div class="toolbar-section center">
      <div v-if="isRunning" class="running-status">
        <div class="status-dot animate-breathe"></div>
        <span class="status-text">回测运行中...</span>
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: progressPercent + '%' }"></div>
        </div>
        <span class="progress-text">{{ progressPercent }}%</span>
      </div>
      
      <div v-else-if="hasBacktestData" class="backtest-summary">
        <span class="summary-item">
          <span class="label">总收益</span>
          <span :class="['value', totalReturn >= 0 ? 'positive' : 'negative']">
            {{ formatPercent(totalReturn) }}
          </span>
        </span>
        <span class="summary-separator">|</span>
        <span class="summary-item">
          <span class="label">夏普比率</span>
          <span class="value">{{ formatNumber(sharpeRatio) }}</span>
        </span>
        <span class="summary-separator">|</span>
        <span class="summary-item">
          <span class="label">最大回撤</span>
          <span class="value negative">{{ formatPercent(maxDrawdown) }}</span>
        </span>
      </div>
    </div>

    <!-- 右侧：操作按钮 -->
    <div class="toolbar-section right">
      <button
        class="toolbar-btn"
        :class="{ 'active': showConfigPanel }"
        @click="showConfigPanel = !showConfigPanel"
        title="回测配置"
      >
        <i class="fas fa-cog"></i>
        <span>配置</span>
      </button>
      
      <button
        class="toolbar-btn"
        @click="$emit('format')"
        title="格式化代码"
      >
        <i class="fas fa-magic"></i>
        <span>格式化</span>
      </button>
      
      <button
        class="toolbar-btn"
        @click="$emit('save')"
        title="保存策略"
      >
        <i class="fas fa-save"></i>
        <span>保存</span>
      </button>
      
      <button
        v-if="!isRunning"
        class="toolbar-btn primary run-btn"
        @click="$emit('run')"
        :disabled="!canRun"
        title="运行回测"
      >
        <i class="fas fa-play"></i>
        <span>运行回测</span>
      </button>
      
      <button
        v-else
        class="toolbar-btn danger stop-btn"
        @click="$emit('stop')"
        title="停止回测"
      >
        <i class="fas fa-stop"></i>
        <span>停止</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';

// ============================================
// Props & Emits
// ============================================
interface Props {
  strategyName: string;
  isRunning: boolean;
  hasBacktestData: boolean;
  progress?: number;
  totalReturn?: number;
  sharpeRatio?: number;
  maxDrawdown?: number;
  hasUnsavedChanges?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  progress: 0,
  totalReturn: 0,
  sharpeRatio: 0,
  maxDrawdown: 0,
  hasUnsavedChanges: false,
});

const emit = defineEmits<{
  run: [];
  stop: [];
  save: [];
  reset: [];
  format: [];
  'update:strategyName': [name: string];
}>();

// ============================================
// 状态
// ============================================
const localStrategyName = ref(props.strategyName);
const showConfigPanel = ref(false);

// ============================================
// 计算属性
// ============================================
const progressPercent = computed(() => Math.round(props.progress * 100));
const canRun = computed(() => !props.isRunning);

// ============================================
// 方法
// ============================================
function updateStrategyName() {
  emit('update:strategyName', localStrategyName.value);
}

function formatPercent(value: number): string {
  if (value === undefined || value === null) return '--';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${(value * 100).toFixed(2)}%`;
}

function formatNumber(value: number): string {
  if (value === undefined || value === null) return '--';
  return value.toFixed(2);
}
</script>

<style scoped>
.workbench-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 48px;
  padding: 0 16px;
  margin: 8px 8px 0;
  background-color: var(--bg-secondary, #1e222d);
  border: 1px solid var(--border-color, #2a2e39);
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  flex-shrink: 0;
}

.toolbar-section {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-section.left {
  flex: 1;
  min-width: 0;
}

.toolbar-section.center {
  flex: 2;
  justify-content: center;
}

.toolbar-section.right {
  flex: 1;
  justify-content: flex-end;
}

/* 面包屑 */
.breadcrumb {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-right: 16px;
  font-size: 11px;
  color: var(--text-muted, #787b86);
  flex-shrink: 0;
}

.breadcrumb-link {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--text-secondary, #b2b5be);
  text-decoration: none;
  transition: color 0.2s;
}

.breadcrumb-link:hover {
  color: var(--text-primary, #d1d4dc);
}

.breadcrumb-separator {
  color: var(--border-hover, #505053);
}

.breadcrumb-current {
  color: var(--text-primary, #d1d4dc);
  font-weight: 500;
}

/* 策略信息 */
.strategy-info {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  background-color: var(--bg-tertiary, #2a2e39);
  border: 1px solid var(--border-color, #2a2e39);
  border-radius: 4px;
  min-width: 0;
}

.strategy-icon {
  font-size: 12px;
  color: var(--accent-primary, #2962ff);
  flex-shrink: 0;
}

.strategy-name-input {
  background: transparent;
  border: none;
  color: var(--text-primary, #d1d4dc);
  font-size: 12px;
  font-weight: 500;
  width: 150px;
  outline: none;
}

.strategy-name-input::placeholder {
  color: var(--text-muted, #787b86);
}

.unsaved-indicator {
  color: var(--accent-warning, #f5a623);
  font-size: 8px;
  flex-shrink: 0;
}

/* 运行状态 */
.running-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  background-color: var(--bg-tertiary, #2a2e39);
  border-radius: 4px;
}

.status-dot {
  width: 6px;
  height: 6px;
  background-color: var(--color-up, #089981);
  border-radius: 50%;
  flex-shrink: 0;
}

.status-text {
  font-size: 11px;
  color: var(--text-secondary, #b2b5be);
  white-space: nowrap;
}

.progress-bar {
  width: 100px;
  height: 4px;
  background-color: var(--border-color, #2a2e39);
  border-radius: 2px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background-color: var(--accent-primary, #2962ff);
  transition: width 0.3s ease;
}

.progress-text {
  font-size: 10px;
  color: var(--text-muted, #787b86);
  font-family: 'JetBrains Mono', monospace;
  min-width: 28px;
  text-align: right;
}

/* 回测摘要 */
.backtest-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 12px;
  background-color: var(--bg-tertiary, #2a2e39);
  border-radius: 4px;
}

.summary-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.summary-item .label {
  font-size: 10px;
  color: var(--text-muted, #787b86);
  text-transform: uppercase;
}

.summary-item .value {
  font-size: 12px;
  font-weight: 600;
  font-family: 'JetBrains Mono', monospace;
}

.summary-item .value.positive {
  color: var(--color-up, #089981);
}

.summary-item .value.negative {
  color: var(--color-down, #f23645);
}

.summary-separator {
  color: var(--border-hover, #3e3e42);
  font-size: 10px;
}

/* 工具栏按钮 */
.toolbar-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 10px;
  height: 28px;
  background-color: var(--bg-tertiary, #2a2e39);
  border: 1px solid var(--border-color, #2a2e39);
  border-radius: 4px;
  color: var(--text-secondary, #b2b5be);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}

.toolbar-btn:hover:not(:disabled) {
  background-color: var(--bg-hover, #3e3e42);
  border-color: var(--border-hover, #3e3e42);
  color: var(--text-primary, #d1d4dc);
}

.toolbar-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.toolbar-btn.active {
  background-color: var(--accent-primary, #2962ff);
  border-color: var(--accent-primary, #2962ff);
  color: white;
}

.toolbar-btn.primary {
  background-color: var(--color-up, #089981);
  border-color: var(--color-up, #089981);
  color: white;
}

.toolbar-btn.primary:hover:not(:disabled) {
  background-color: var(--color-up-light, #0db89a);
  border-color: var(--color-up-light, #0db89a);
}

.toolbar-btn.danger {
  background-color: var(--color-down, #f23645);
  border-color: var(--color-down, #f23645);
  color: white;
}

.toolbar-btn.danger:hover {
  background-color: var(--color-down-light, #f55763);
  border-color: var(--color-down-light, #f55763);
}

.toolbar-btn i {
  font-size: 10px;
}

.run-btn i,
.stop-btn i {
  font-size: 8px;
}

/* 响应式 */
@media (max-width: 991px) {
  .breadcrumb {
    display: none;
  }
  
  .backtest-summary {
    gap: 8px;
    padding: 4px 8px;
  }
  
  .summary-item .label {
    display: none;
  }
  
  .toolbar-btn span {
    display: none;
  }
  
  .toolbar-btn {
    padding: 0 8px;
  }
}

@media (max-width: 767px) {
  .workbench-toolbar {
    flex-wrap: wrap;
    height: auto;
    padding: 8px;
    gap: 8px;
  }
  
  .toolbar-section {
    width: 100%;
    justify-content: center;
  }
  
  .toolbar-section.center {
    order: -1;
  }
}
</style>
