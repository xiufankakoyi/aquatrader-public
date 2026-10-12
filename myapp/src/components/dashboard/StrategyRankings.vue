<template>
  <div class="strategy-rankings">
    <div class="rankings-header">
      <h3 class="rankings-title">
        <i class="fas fa-trophy"></i>
        策略天梯
      </h3>
      <span class="rankings-count">{{ strategies.length }} 个策略</span>
    </div>
    
    <div class="rankings-list">
      <div
        v-for="strategy in sortedStrategies"
        :key="strategy.id"
        class="strategy-card"
        :class="{ 'active': selectedStrategy?.id === strategy.id }"
        @click="selectStrategy(strategy)"
      >
        <div class="strategy-main">
          <div class="strategy-info">
            <span class="strategy-name">{{ strategy.name }}</span>
            <span 
              class="strategy-status"
              :class="strategy.status"
            >
              <span class="status-dot"></span>
              {{ getStatusLabel(strategy.status) }}
            </span>
          </div>
          
          <div class="strategy-metrics">
            <div class="metric">
              <span class="metric-label">今日收益</span>
              <span 
                class="metric-value font-mono"
                :class="strategy.todayReturn >= 0 ? 'text-up' : 'text-down'"
              >
                {{ formatSignedPercent(strategy.todayReturn) }}
              </span>
            </div>
            
            <div class="metric">
              <span class="metric-label">夏普比率</span>
              <span class="metric-value font-mono">{{ strategy.sharpeRatio.toFixed(2) }}</span>
            </div>
          </div>
        </div>
        
        <div class="strategy-arrow">
          <i class="fas fa-chevron-right"></i>
        </div>
      </div>
    </div>
    
    <div v-if="strategies.length === 0" class="empty-state">
      <i class="fas fa-robot"></i>
      <p>暂无策略</p>
      <button class="create-btn" @click="$emit('create')">
        <i class="fas fa-plus"></i>
        创建策略
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

interface Strategy {
  id: string;
  name: string;
  status: 'running' | 'stopped' | 'error';
  todayReturn: number;
  sharpeRatio: number;
  totalReturn: number;
  lastRunAt?: string;
}

interface Props {
  strategies: Strategy[];
  selectedStrategy?: Strategy | null;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  select: [strategy: Strategy];
  create: [];
}>();

const sortedStrategies = computed(() => {
  return [...props.strategies].sort((a, b) => {
    // 运行中的策略排在前面
    if (a.status === 'running' && b.status !== 'running') return -1;
    if (b.status === 'running' && a.status !== 'running') return 1;
    // 按今日收益排序
    return b.todayReturn - a.todayReturn;
  });
});

function selectStrategy(strategy: Strategy) {
  emit('select', strategy);
}

function getStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: '运行中',
    stopped: '已停止',
    error: '错误',
  };
  return labels[status] || status;
}

function formatSignedPercent(value: number): string {
  if (value === undefined || value === null) return '0.00%';
  const sign = value >= 0 ? '+' : '';
  return sign + (value * 100).toFixed(2) + '%';
}
</script>

<style scoped>
.strategy-rankings {
  display: flex;
  flex-direction: column;
  height: 100%;
  background-color: var(--bg-secondary);
  border-right: 1px solid var(--border-color);
}

.rankings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color);
}

.rankings-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.rankings-title i {
  color: #f5a623;
}

.rankings-count {
  font-size: 11px;
  color: var(--text-muted);
}

.rankings-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.strategy-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  margin-bottom: 8px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.strategy-card:hover {
  border-color: var(--border-hover);
  background-color: var(--bg-hover);
}

.strategy-card.active {
  border-color: var(--accent-primary);
  background-color: rgba(41, 98, 255, 0.1);
}

.strategy-main {
  flex: 1;
  min-width: 0;
}

.strategy-info {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.strategy-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.strategy-status {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 500;
  white-space: nowrap;
}

.strategy-status.running {
  background-color: rgba(8, 153, 129, 0.15);
  color: var(--color-down);
}

.strategy-status.stopped {
  background-color: rgba(120, 123, 134, 0.15);
  color: var(--text-muted);
}

.strategy-status.error {
  background-color: rgba(242, 54, 69, 0.15);
  color: var(--color-up);
}

.status-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
}

.strategy-status.running .status-dot {
  background-color: var(--color-down);
  animation: pulse 2s infinite;
}

.strategy-status.stopped .status-dot {
  background-color: var(--text-muted);
}

.strategy-status.error .status-dot {
  background-color: var(--color-up);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.strategy-metrics {
  display: flex;
  gap: 16px;
}

.metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.metric-label {
  font-size: 10px;
  color: var(--text-muted);
}

.metric-value {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.text-up {
  color: var(--color-up);
}

.text-down {
  color: var(--color-down);
}

.strategy-arrow {
  color: var(--text-muted);
  font-size: 11px;
  transition: color 0.2s;
}

.strategy-card:hover .strategy-arrow {
  color: var(--text-primary);
}

.font-mono {
  font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
  font-variant-numeric: tabular-nums;
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 24px;
  color: var(--text-muted);
}

.empty-state i {
  font-size: 32px;
  opacity: 0.5;
}

.empty-state p {
  font-size: 12px;
}

.create-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  background-color: var(--accent-primary);
  border: none;
  border-radius: 4px;
  color: white;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.2s;
}

.create-btn:hover {
  background-color: var(--accent-primary-hover);
}

/* 响应式 */
@media (max-width: 767px) {
  .strategy-rankings {
    border-right: none;
    border-bottom: 1px solid var(--border-color);
  }
  
  .strategy-metrics {
    gap: 12px;
  }
}
</style>
