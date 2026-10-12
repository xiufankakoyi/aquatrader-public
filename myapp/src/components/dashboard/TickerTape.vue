<template>
  <div class="ticker-tape">
    <div class="ticker-content">
      <!-- 总资产 -->
      <div class="ticker-item">
        <span class="ticker-label">总资产</span>
        <span class="ticker-value font-mono">{{ formatCurrency(totalAssets) }}</span>
      </div>
      
      <!-- 今日盈亏 -->
      <div class="ticker-item">
        <span class="ticker-label">今日盈亏</span>
        <span 
          class="ticker-value font-mono"
          :class="pnlToday >= 0 ? 'text-up' : 'text-down'"
        >
          {{ formatSignedCurrency(pnlToday) }}
        </span>
      </div>
      
      <!-- 今日盈亏百分比 -->
      <div class="ticker-item">
        <span class="ticker-label">今日涨跌</span>
        <span 
          class="ticker-value font-mono"
          :class="pnlTodayPercent >= 0 ? 'text-up' : 'text-down'"
        >
          {{ formatSignedPercent(pnlTodayPercent) }}
        </span>
      </div>
      
      <!-- 总仓位占比 -->
      <div class="ticker-item">
        <span class="ticker-label">总仓位</span>
        <div class="exposure-bar">
          <div 
            class="exposure-fill"
            :style="{ width: exposure + '%' }"
            :class="getExposureClass(exposure)"
          ></div>
        </div>
        <span class="ticker-value font-mono">{{ exposure.toFixed(1) }}%</span>
      </div>
      
      <!-- 最大回撤 -->
      <div class="ticker-item">
        <span class="ticker-label">最大回撤</span>
        <span class="ticker-value font-mono text-down">
          {{ formatPercent(maxDrawdown) }}
        </span>
      </div>
      
      <!-- 夏普比率 -->
      <div class="ticker-item">
        <span class="ticker-label">夏普比率</span>
        <span class="ticker-value font-mono">{{ sharpeRatio.toFixed(2) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  totalAssets: number;
  pnlToday: number;
  pnlTodayPercent: number;
  exposure: number;
  maxDrawdown: number;
  sharpeRatio: number;
}

withDefaults(defineProps<Props>(), {
  totalAssets: 0,
  pnlToday: 0,
  pnlTodayPercent: 0,
  exposure: 0,
  maxDrawdown: 0,
  sharpeRatio: 0,
});

function formatCurrency(value: number): string {
  if (value === undefined || value === null) return '¥0.00';
  return '¥' + value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatSignedCurrency(value: number): string {
  if (value === undefined || value === null) return '¥0.00';
  const sign = value >= 0 ? '+' : '';
  return sign + '¥' + Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatSignedPercent(value: number): string {
  if (value === undefined || value === null) return '0.00%';
  const sign = value >= 0 ? '+' : '';
  return sign + (value * 100).toFixed(2) + '%';
}

function formatPercent(value: number): string {
  if (value === undefined || value === null) return '0.00%';
  return (value * 100).toFixed(2) + '%';
}

function getExposureClass(exposure: number): string {
  if (exposure >= 80) return 'high';
  if (exposure >= 50) return 'medium';
  return 'low';
}
</script>

<style scoped>
.ticker-tape {
  height: 56px;
  background-color: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  padding: 0 16px;
  overflow-x: auto;
}

.ticker-content {
  display: flex;
  align-items: center;
  gap: 32px;
  min-width: max-content;
}

.ticker-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ticker-label {
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.ticker-value {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
}

.text-up {
  color: var(--color-up);
}

.text-down {
  color: var(--color-down);
}

.exposure-bar {
  width: 60px;
  height: 4px;
  background-color: var(--bg-tertiary);
  border-radius: 2px;
  overflow: hidden;
  margin-top: 4px;
}

.exposure-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s ease;
}

.exposure-fill.low {
  background-color: var(--color-down);
}

.exposure-fill.medium {
  background-color: #f5a623;
}

.exposure-fill.high {
  background-color: var(--color-up);
}

.font-mono {
  font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
  font-variant-numeric: tabular-nums;
}

/* 响应式 */
@media (max-width: 991px) {
  .ticker-tape {
    height: 48px;
    padding: 0 12px;
  }
  
  .ticker-content {
    gap: 20px;
  }
  
  .ticker-value {
    font-size: 14px;
  }
}
</style>
