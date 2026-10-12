<template>
  <div class="mobile-dashboard">
    <!-- 来源提示 -->
    <div v-if="fromWorkbench" class="md-source-banner">
      <i class="fa-solid fa-code"></i>
      <span v-if="loading">加载中...</span>
      <span v-else-if="loadError">{{ loadError }}</span>
      <span v-else>来自策略工作台的回测</span>
    </div>

    <!-- 无数据状态 -->
    <div v-if="!hasData && !loading" class="md-empty-state">
      <i class="fa-solid fa-chart-line"></i>
      <p>暂无回测数据</p>
      <button class="md-btn-primary" @click="goToWorkbench">去策略工作台</button>
    </div>

    <template v-if="hasData">
      <!-- 策略概览 -->
      <section class="md-card md-overview">
        <div class="md-overview-name">{{ displayName }}</div>
        <div class="md-overview-range">{{ displayRange }}</div>
      </section>

      <!-- KPI 卡片网格 -->
      <section class="md-kpi-grid">
        <div class="md-card md-kpi">
          <span class="md-kpi-label">累计收益</span>
          <strong class="md-kpi-value" :class="{ positive: totalReturn > 0, negative: totalReturn < 0 }">
            {{ formatPercent(metrics?.totalReturn) }}
          </strong>
        </div>
        <div class="md-card md-kpi">
          <span class="md-kpi-label">最大回撤</span>
          <strong class="md-kpi-value negative">{{ formatPercent(metrics?.maxDrawdown) }}</strong>
        </div>
        <div class="md-card md-kpi">
          <span class="md-kpi-label">夏普比率</span>
          <strong class="md-kpi-value">{{ formatScore(metrics?.sharpeRatio) }}</strong>
        </div>
        <div class="md-card md-kpi">
          <span class="md-kpi-label">胜率</span>
          <strong class="md-kpi-value">{{ tradeCount > 0 ? formatPercent(metrics?.winRate) : 'N/A' }}</strong>
        </div>
        <div class="md-card md-kpi">
          <span class="md-kpi-label">交易次数</span>
          <strong class="md-kpi-value">{{ tradeCount }}</strong>
        </div>
        <div class="md-card md-kpi">
          <span class="md-kpi-label">波动率</span>
          <strong class="md-kpi-value">{{ formatPercent(metrics?.volatility) }}</strong>
        </div>
      </section>

      <!-- 净值曲线 -->
      <section class="md-card md-chart-card">
        <div class="md-card-title">
          <i class="fa-solid fa-chart-line"></i>
          <span>净值曲线</span>
        </div>
        <div class="md-chart-wrap">
          <svg :viewBox="`0 0 ${chartWidth} ${chartHeight}`" class="md-chart-svg" preserveAspectRatio="none">
            <!-- 基准线 0% -->
            <line :x1="0" :y1="chartHeight / 2" :x2="chartWidth" :y2="chartHeight / 2" stroke="#3c3c3c" stroke-dasharray="4 4" />
            <!-- 净值曲线 -->
            <polyline
              v-if="equityPath"
              :points="equityPath"
              fill="none"
              stroke="#4c7fff"
              stroke-width="2"
              stroke-linejoin="round"
              stroke-linecap="round"
            />
          </svg>
          <div class="md-chart-meta">
            <span>起点: {{ formatEquity(equitySeries[0]?.equity) }}</span>
            <span>终点: {{ formatEquity(equitySeries[equitySeries.length - 1]?.equity) }}</span>
          </div>
        </div>
      </section>

      <!-- 标签切换 -->
      <section class="md-tabs">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="md-tab-btn"
          :class="{ active: activeTab === tab.key }"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
          <span v-if="tab.key === 'trades' && trades.length" class="md-tab-badge">{{ trades.length }}</span>
        </button>
      </section>

      <!-- 交易明细 -->
      <section v-if="activeTab === 'trades'" class="md-card md-tab-content">
        <div v-if="!trades.length" class="md-empty-inline">暂无交易记录</div>
        <ul v-else class="md-trade-list">
          <li
            v-for="trade in trades"
            :key="trade.id || `${trade.date}-${trade.symbolCode || trade.symbol}`"
            class="md-trade-item"
            :class="`action-${trade.action}`"
          >
            <div class="md-trade-main">
              <span class="md-trade-action">{{ tradeActionText(trade.action) }}</span>
              <span class="md-trade-code">{{ trade.symbolCode || trade.symbol }}</span>
            </div>
            <div class="md-trade-meta">
              <span class="md-trade-date">{{ trade.date }}</span>
              <span class="md-trade-price">{{ formatAmount(trade.price) }}</span>
              <span v-if="trade.quantity" class="md-trade-qty">×{{ trade.quantity }}</span>
            </div>
            <div v-if="trade.profitLoss != null" class="md-trade-pnl" :class="{ positive: trade.profitLoss > 0, negative: trade.profitLoss < 0 }">
              {{ formatAmount(trade.profitLoss) }}
            </div>
          </li>
        </ul>
      </section>

      <!-- 指标详情 -->
      <section v-else-if="activeTab === 'metrics'" class="md-card md-tab-content">
        <dl class="md-metric-list">
          <div><dt>累计收益</dt><dd>{{ formatPercent(metrics?.totalReturn) }}</dd></div>
          <div><dt>年化收益</dt><dd>{{ formatPercent(metrics?.annualReturn) }}</dd></div>
          <div><dt>最大回撤</dt><dd>{{ formatPercent(metrics?.maxDrawdown) }}</dd></div>
          <div><dt>夏普比率</dt><dd>{{ formatScore(metrics?.sharpeRatio) }}</dd></div>
          <div><dt>波动率</dt><dd>{{ formatPercent(metrics?.volatility) }}</dd></div>
          <div><dt>胜率</dt><dd>{{ tradeCount > 0 ? formatPercent(metrics?.winRate) : 'N/A' }}</dd></div>
          <div><dt>盈亏比</dt><dd>{{ tradeCount > 0 ? formatScore(metrics?.profitLossRatio) : 'N/A' }}</dd></div>
          <div><dt>交易次数</dt><dd>{{ tradeCount }}</dd></div>
        </dl>
      </section>

      <!-- 回撤曲线 -->
      <section v-else-if="activeTab === 'drawdown'" class="md-card md-tab-content">
        <div class="md-chart-wrap">
          <svg :viewBox="`0 0 ${chartWidth} ${chartHeight}`" class="md-chart-svg" preserveAspectRatio="none">
            <line :x1="0" :y1="0" :x2="chartWidth" :y2="0" stroke="#3c3c3c" stroke-dasharray="4 4" />
            <polyline
              v-if="drawdownPath"
              :points="drawdownPath"
              fill="none"
              stroke="#f48771"
              stroke-width="2"
              stroke-linejoin="round"
              stroke-linecap="round"
            />
          </svg>
          <div class="md-chart-meta">
            <span>最大回撤: {{ formatPercent(metrics?.maxDrawdown) }}</span>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * MobileDashboard - 移动端回测看板
 *
 * 展示策略工作台回测结果：
 * - KPI 卡片（累计收益、最大回撤、夏普比率、胜率等）
 * - 净值曲线（SVG 简化版）
 * - 交易明细（卡片列表）
 * - 指标详情
 *
 * 数据来源：backtestStore（与桌面端共享）
 */
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useBacktestStore } from '../../store/backtestStore';
import { apiService } from '../../services/api';
import type { Trade } from '../../types/backtest';

const route = useRoute();
const router = useRouter();
const backtestStore = useBacktestStore();

const fromWorkbench = computed(() => route.query.from === 'workbench');
const backtestIdFromQuery = computed(() => (route.query.backtest_id as string) || '');

const loading = ref(false);
const loadError = ref('');
const activeTab = ref<'trades' | 'metrics' | 'drawdown'>('trades');

const tabs = [
  { key: 'trades' as const, label: '交易明细' },
  { key: 'metrics' as const, label: '指标' },
  { key: 'drawdown' as const, label: '回撤' },
];

// ============================================
// 计算属性
// ============================================

const metrics = computed(() => backtestStore.metrics);
const equitySeries = computed(() => backtestStore.equitySeries);
const trades = computed(() => backtestStore.trades as Trade[]);
const hasData = computed(() => equitySeries.value.length > 0 || metrics.value !== null);
const tradeCount = computed(() => metrics.value?.tradesCount ?? metrics.value?.totalTrades ?? trades.value.length);
const totalReturn = computed(() => metrics.value?.totalReturn ?? 0);

const displayName = computed(() => backtestStore.lastRunParams?.strategyName || '未命名策略');
const displayRange = computed(() => {
  const p = backtestStore.lastRunParams;
  return p ? `${p.startDate} ~ ${p.endDate}` : '';
});

// ============================================
// 图表计算（SVG 路径）
// ============================================

const chartWidth = 320;
const chartHeight = 140;

const equityPath = computed(() => {
  if (!equitySeries.value.length) return '';
  const data = equitySeries.value;
  const values = data.map(p => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = chartWidth / Math.max(1, data.length - 1);
  return data.map((p, i) => {
    const x = i * stepX;
    const y = chartHeight - ((p.equity - min) / range) * chartHeight;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
});

const drawdownPath = computed(() => {
  if (!equitySeries.value.length) return '';
  const data = equitySeries.value;
  let peak = data[0].equity;
  const drawdowns = data.map(p => {
    if (p.equity > peak) peak = p.equity;
    return peak > 0 ? (p.equity - peak) / peak : 0;
  });
  const min = Math.min(...drawdowns);
  const range = Math.abs(min) || 1;
  const stepX = chartWidth / Math.max(1, data.length - 1);
  return drawdowns.map((d, i) => {
    const x = i * stepX;
    const y = (Math.abs(d) / range) * chartHeight;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
});

// ============================================
// 格式化工具
// ============================================

function formatPercent(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return 'N/A';
  const normalized = Math.abs(value) <= 1 ? value * 100 : value;
  return `${normalized.toFixed(2)}%`;
}

function formatScore(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return 'N/A';
  return value.toFixed(4);
}

function formatAmount(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return value.toFixed(2);
}

function formatEquity(value?: number): string {
  if (value == null) return 'N/A';
  return formatAmount(value);
}

function tradeActionText(action: string): string {
  const map: Record<string, string> = {
    buy: '买入',
    sell: '卖出',
    hold: '持有',
  };
  return map[action] || action;
}

// ============================================
// 后端拉取（store 为空时）
// ============================================

async function fetchBacktestById(jobId: string) {
  loading.value = true;
  loadError.value = '';
  try {
    const job = await apiService.getBacktestJob(jobId);
    if (job.status === 'success') {
      if (job.equity_curve && job.equity_curve.length) {
        backtestStore.addEquityPoints(job.equity_curve.map(p => ({ date: p.date, equity: p.equity })));
      }
      if (job.stats) {
        const s = job.stats;
        backtestStore.setMetrics({
          totalReturn: s.total_return ?? s.totalReturn ?? 0,
          annualizedReturn: s.annual_return ?? s.annualReturn ?? 0,
          maxDrawdown: s.max_drawdown ?? s.maxDrawdown ?? 0,
          sharpeRatio: s.sharpe_ratio ?? s.sharpeRatio ?? 0,
          volatility: s.volatility ?? 0,
          winRate: s.win_rate ?? s.winRate ?? 0,
          profitLossRatio: s.profit_loss_ratio ?? s.profitLossRatio ?? 0,
          tradesCount: s.total_trades ?? s.totalTrades ?? 0,
        } as any);
      }
      backtestStore.persistToStorage();
    } else if (job.status === 'failed') {
      loadError.value = job.message || '回测任务失败';
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '拉取回测结果失败';
  } finally {
    loading.value = false;
  }
}

function goToWorkbench() {
  router.push('/mobile/strategy');
}

// ============================================
// 生命周期
// ============================================

onMounted(() => {
  backtestStore.hydrateFromStorage();
  if (fromWorkbench.value && backtestIdFromQuery.value && !hasData.value) {
    fetchBacktestById(backtestIdFromQuery.value);
  }
});
</script>

<style scoped>
.mobile-dashboard {
  display: flex;
  flex-direction: column;
  gap: 12px;
  color: #cccccc;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

/* 来源提示横幅 */
.md-source-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: rgba(76, 127, 255, 0.1);
  border: 1px solid rgba(76, 127, 255, 0.3);
  border-radius: 8px;
  font-size: 12px;
  color: #4c7fff;
}

.md-source-banner i {
  font-size: 14px;
}

/* 空状态 */
.md-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 16px;
  text-align: center;
  color: #6e6e6e;
}

.md-empty-state i {
  font-size: 48px;
  opacity: 0.5;
}

.md-empty-state p {
  margin: 0;
  font-size: 14px;
}

.md-btn-primary {
  padding: 10px 20px;
  background: #007acc;
  color: #ffffff;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

.md-btn-primary:active {
  background: #005a8b;
}

/* 卡片 */
.md-card {
  background: #1e1e1e;
  border: 1px solid #2d2d2d;
  border-radius: 10px;
  overflow: hidden;
}

/* 概览卡片 */
.md-overview {
  padding: 14px 16px;
}

.md-overview-name {
  font-size: 16px;
  font-weight: 600;
  color: #ffffff;
  margin-bottom: 4px;
}

.md-overview-range {
  font-size: 12px;
  color: #888888;
}

/* KPI 网格 */
.md-kpi-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.md-kpi {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.md-kpi-label {
  font-size: 11px;
  color: #888888;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.md-kpi-value {
  font-size: 18px;
  font-weight: 700;
  color: #ffffff;
}

.md-kpi-value.positive {
  color: #89d185;
}

.md-kpi-value.negative {
  color: #f48771;
}

/* 图表卡片 */
.md-chart-card {
  padding: 12px;
}

.md-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #cccccc;
  margin-bottom: 10px;
}

.md-card-title i {
  color: #4c7fff;
  font-size: 14px;
}

.md-chart-wrap {
  position: relative;
}

.md-chart-svg {
  width: 100%;
  height: 140px;
  display: block;
}

.md-chart-meta {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  font-size: 11px;
  color: #888888;
}

/* 标签 */
.md-tabs {
  display: flex;
  background: #1e1e1e;
  border: 1px solid #2d2d2d;
  border-radius: 8px;
  overflow: hidden;
}

.md-tab-btn {
  flex: 1;
  padding: 10px 8px;
  background: transparent;
  border: none;
  color: #888888;
  font-size: 13px;
  cursor: pointer;
  position: relative;
  -webkit-tap-highlight-color: transparent;
}

.md-tab-btn.active {
  color: #ffffff;
  background: #2d2d2d;
}

.md-tab-btn.active::after {
  content: '';
  position: absolute;
  left: 20%;
  right: 20%;
  bottom: 0;
  height: 2px;
  background: #4c7fff;
}

.md-tab-badge {
  display: inline-block;
  margin-left: 4px;
  padding: 1px 6px;
  background: #3c3c3c;
  color: #cccccc;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  min-width: 18px;
  text-align: center;
}

.md-tab-content {
  padding: 12px;
}

.md-empty-inline {
  text-align: center;
  color: #6e6e6e;
  font-style: italic;
  padding: 16px 0;
  font-size: 13px;
}

/* 交易列表 */
.md-trade-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.md-trade-item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 10px;
  background: #252526;
  border-radius: 6px;
  border-left: 3px solid transparent;
  font-size: 12px;
}

.md-trade-item.action-buy {
  border-left-color: #89d185;
}

.md-trade-item.action-sell {
  border-left-color: #f48771;
}

.md-trade-item.action-hold {
  border-left-color: #888888;
}

.md-trade-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.md-trade-action {
  font-size: 11px;
  color: #888888;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.action-buy .md-trade-action {
  color: #89d185;
}

.action-sell .md-trade-action {
  color: #f48771;
}

.md-trade-code {
  font-weight: 600;
  color: #ffffff;
  font-size: 13px;
}

.md-trade-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: #888888;
  text-align: right;
}

.md-trade-date {
  color: #cccccc;
}

.md-trade-price {
  color: #ffffff;
  font-weight: 600;
}

.md-trade-qty {
  color: #888888;
}

.md-trade-pnl {
  font-weight: 700;
  font-size: 13px;
  text-align: right;
}

.md-trade-pnl.positive {
  color: #89d185;
}

.md-trade-pnl.negative {
  color: #f48771;
}

/* 指标列表 */
.md-metric-list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.md-metric-list div {
  display: flex;
  justify-content: space-between;
  padding: 10px 0;
  border-bottom: 1px solid #2d2d2d;
  font-size: 13px;
}

.md-metric-list div:last-child {
  border-bottom: none;
}

.md-metric-list dt {
  color: #888888;
}

.md-metric-list dd {
  margin: 0;
  color: #ffffff;
  font-weight: 600;
}
</style>
