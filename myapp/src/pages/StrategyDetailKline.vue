<template>
  <div class="strategy-detail-page">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <!-- A. 左侧：策略信息 -->
      <div class="toolbar-left">
        <div class="strategy-info">
          <h2 class="strategy-name">
            {{ strategyStore.currentVersion?.name || strategyId || '策略详情' }}
          </h2>
          <span class="strategy-badge" v-if="hasBacktestData">回测模式</span>
        </div>

        <div class="divider"></div>

        <!-- 图表缩放控制 -->
        <div class="chart-scale">
          <span class="scale-label">缩放:</span>
          <button
            @click="chartScale = 'linear'"
            :class="['scale-btn', { 'active': chartScale === 'linear' }]"
          >线性</button>
          <button
            @click="chartScale = 'log'"
            :class="['scale-btn', { 'active': chartScale === 'log' }]"
          >对数</button>
        </div>
      </div>

      <!-- B. 中间：核心指标 -->
      <div class="toolbar-center">
        <div class="metric-item" v-if="backtestStore.metrics">
          <span class="metric-label">总收益</span>
          <span class="metric-value" :class="backtestStore.metrics.totalReturn >= 0 ? 'positive' : 'negative'">
            {{ formatPercent(backtestStore.metrics.totalReturn) }}
          </span>
        </div>
        <div class="metric-item" v-if="backtestStore.metrics">
          <span class="metric-label">最大回撤</span>
          <span class="metric-value negative">{{ formatPercent(backtestStore.metrics.maxDrawdown, true) }}</span>
        </div>
        <div class="metric-item" v-if="backtestStore.metrics">
          <span class="metric-label">夏普比率</span>
          <span class="metric-value">{{ backtestStore.metrics.sharpeRatio?.toFixed(2) || 'N/A' }}</span>
        </div>
        <div class="metric-item" v-if="backtestStore.metrics">
          <span class="metric-label">胜率</span>
          <span class="metric-value">{{ formatPercent(backtestStore.metrics.winRate) }}</span>
        </div>
      </div>

      <!-- C. 右侧：操作按钮 -->
      <div class="toolbar-right">
        <button @click="runBacktest" class="run-btn primary" :disabled="isBacktestRunning">
          <i class="fas fa-play"></i>
          <span>{{ isBacktestRunning ? '运行中...' : '运行回测' }}</span>
        </button>

        <button
          @click="toggleTradePanel"
          :class="['toolbar-icon-btn', { 'active': isTradePanelExpanded }]"
          title="交易明细"
        >
          <i class="fas fa-exchange-alt"></i>
        </button>

        <button
          @click="toggleFullscreen"
          class="toolbar-icon-btn"
          title="全屏"
        >
          <i :class="['fas', isFullscreen ? 'fa-compress' : 'fa-expand']"></i>
        </button>
      </div>
    </div>

    <LoadingState
      v-if="strategyStore.isLoading && !strategyStore.currentBacktestResult"
      title="正在加载策略详情"
    />
    <ErrorState
      v-else-if="strategyStore.error"
      :message="strategyStore.error"
      @retry="loadStrategyDetail"
    />

    <!-- 主要内容区域 -->
    <div v-else class="main-area">
      <!-- 左侧图表区域：净值曲线 + 回撤 + 交易频率 -->
      <div class="charts-container" v-if="hasBacktestData">
        <!-- 净值曲线 -->
        <div 
          class="chart-pane equity-pane" 
          :style="{ height: equityPaneHeight + 'px' }"
        >
          <div class="pane-header">
            <span class="pane-label">净值曲线</span>
          </div>
          <div class="pane-content">
            <EquityCurve
              :versions="equityCurveData"
              :benchmark="benchmarkData"
              :scale="chartScale"
              :sync-x-axis="syncDate"
              @hover="handleChartHover"
            />
          </div>
          <!-- 拖动调整高度手柄 -->
          <div 
            class="pane-resize-handle"
            @mousedown="startPaneResize($event, 'equity')"
          >
            <div class="pane-resize-indicator"></div>
          </div>
        </div>

        <!-- 回撤序列 -->
        <div 
          class="chart-pane drawdown-pane"
          :style="{ height: isDrawdownCollapsed ? '32px' : drawdownPaneHeight + 'px' }"
        >
          <div class="pane-header" @click="toggleDrawdownPane">
            <div class="header-left">
              <i :class="['fas', isDrawdownCollapsed ? 'fa-chevron-right' : 'fa-chevron-down', 'collapse-icon']"></i>
              <span class="pane-label">回撤序列</span>
            </div>
            <button class="pane-action-btn" @click.stop="isDrawdownCollapsed = !isDrawdownCollapsed">
              <i :class="['fas', isDrawdownCollapsed ? 'fa-expand' : 'fa-compress']"></i>
            </button>
          </div>
          <div class="pane-content" v-show="!isDrawdownCollapsed">
            <DrawdownChart
              :equity-series="backtestStore.equitySeries"
              :sync-x-axis="syncDate"
            />
          </div>
        </div>

        <!-- 交易频率 -->
        <div 
          class="chart-pane frequency-pane"
          :style="{ height: isFrequencyCollapsed ? '32px' : frequencyPaneHeight + 'px' }"
        >
          <div class="pane-header" @click="toggleFrequencyPane">
            <div class="header-left">
              <i :class="['fas', isFrequencyCollapsed ? 'fa-chevron-right' : 'fa-chevron-down', 'collapse-icon']"></i>
              <span class="pane-label">交易频率</span>
            </div>
            <button class="pane-action-btn" @click.stop="isFrequencyCollapsed = !isFrequencyCollapsed">
              <i :class="['fas', isFrequencyCollapsed ? 'fa-expand' : 'fa-compress']"></i>
            </button>
          </div>
          <div class="pane-content" v-show="!isFrequencyCollapsed">
            <TradeFrequencyChart
              :trades="backtestStore.trades"
              :sync-x-axis="syncDate"
            />
          </div>
        </div>
      </div>

      <!-- 无数据状态 -->
      <div class="empty-state" v-else>
        <EmptyState title="暂无回测数据" description="当前策略没有可展示的真实回测曲线" />
        <button @click="runBacktest" class="empty-action-btn" :disabled="isBacktestRunning">
          <i class="fas fa-play"></i>
          <span>{{ isBacktestRunning ? '运行中...' : '立即运行回测' }}</span>
        </button>
      </div>

      <!-- 右侧持仓面板 -->
      <div class="position-sidebar" v-if="hasBacktestData">
        <PositionCard
          :holding-periods="mappedHoldingPeriods"
          :current-date="currentDate"
          :latest-prices="latestPrices"
          :total-equity="currentEquity"
          :stock-names="stockNames"
          @analyze="handlePositionAnalyze"
        />
      </div>
    </div>

    <section class="quality-auth-section">
      <div class="report-panel">
        <div class="report-heading">
          <div>
            <h3>策略质量报告</h3>
            <p>{{ qualityPeriod }}</p>
          </div>
          <button class="refresh-report-btn" :disabled="qualityLoading" @click="loadStrategyQuality">
            {{ qualityLoading ? '评估中' : '重新评估' }}
          </button>
        </div>
        <LoadingState v-if="qualityLoading" title="正在评估策略质量" />
        <ErrorState v-else-if="qualityError" :message="qualityError" @retry="loadStrategyQuality" />
        <EmptyState v-else-if="!qualityReport" title="暂无策略质量报告" description="质量接口未返回有效结果" />
        <template v-else>
          <div class="quality-grid">
            <article v-for="item in qualityDimensions" :key="item.key">
              <span>{{ item.label }}</span>
              <strong>{{ formatScore(item.dimension?.score) }}</strong>
              <small>{{ item.dimension?.summary || 'unavailable' }}</small>
            </article>
          </div>
          <dl class="report-summary">
            <div><dt>最终等级</dt><dd>{{ qualityReport.grade || 'unavailable' }}</dd></div>
            <div><dt>综合分数</dt><dd>{{ formatScore(qualityReport.total_score) }}</dd></div>
            <div><dt>警告原因</dt><dd>{{ qualityWarningReason }}</dd></div>
          </dl>
        </template>
      </div>

      <div class="report-panel">
        <div class="report-heading">
          <div>
            <h3>回测真实性</h3>
            <p>缺失字段显示 unavailable，不从历史指标补零。</p>
          </div>
        </div>
        <EmptyState
          v-if="!authenticityAvailable"
          title="暂无真实性统计"
          description="当前回测结果未返回 rejectedOrders、slippageCost 或 filterStats"
        />
        <template v-else>
          <dl class="report-summary two-columns">
            <div><dt>拒单总数</dt><dd>{{ rejectedOrderTotal ?? 'unavailable' }}</dd></div>
            <div><dt>滑点成本</dt><dd>{{ formatOptionalAmount(authenticity.slippageCost) }}</dd></div>
            <div><dt>成交率</dt><dd>{{ formatPercent(fillRate) }}</dd></div>
            <div><dt>拒单率</dt><dd>{{ formatPercent(rejectRate) }}</dd></div>
            <div><dt>理论收益</dt><dd>{{ formatPercent(theoreticalReturn) }}</dd></div>
            <div><dt>真实成交收益</dt><dd>{{ formatPercent(realisticFilledReturn) }}</dd></div>
          </dl>
          <div class="reason-distribution">
            <strong>拒单原因分布</strong>
            <span v-if="rejectedReasonItems.length === 0">unavailable</span>
            <span v-for="item in rejectedReasonItems" :key="item.key">{{ item.label }}：{{ item.value }}</span>
          </div>
          <div class="reason-distribution">
            <strong>过滤参数</strong>
            <span v-if="filterStatItems.length === 0">unavailable</span>
            <span v-for="item in filterStatItems" :key="item.key">{{ item.label }}：{{ item.value }}</span>
          </div>
        </template>
      </div>
    </section>

    <!-- 回放控制器 -->
    <PlaybackController v-if="hasBacktestData && backtestStore.equitySeries.length > 0" />

    <!-- 底部交易记录面板 -->
    <div
      class="trade-panel"
      :style="{ height: isTradePanelExpanded ? `${tradePanelHeight}px` : '40px' }"
    >
      <div
        class="resize-handle"
        @mousedown="startResizing"
      >
        <div class="resize-indicator"></div>
      </div>

      <div class="panel-header" @click="toggleTradePanel">
        <div class="header-left">
          <i :class="['fas', isTradePanelExpanded ? 'fa-chevron-down' : 'fa-chevron-up', 'toggle-icon']"></i>
          <h3 class="panel-title">交易明细 ({{ trades.length }} 笔)</h3>
        </div>

        <div v-if="strategyStore.isLoading" class="loading-indicator">
          <i class="fas fa-spinner fa-spin"></i>
          <span>加载中...</span>
        </div>
      </div>

      <div v-show="isTradePanelExpanded" class="panel-content">
        <div v-if="strategyStore.error" class="error-message">
          <i class="fas fa-exclamation-circle"></i>{{ strategyStore.error }}
        </div>

        <TradeTable
          v-if="trades.length > 0"
          :trades="trades"
          :highlight-date="normalizeDate(playbackCursor)"
          @trade-select="handleTradeSelect"
        />

        <div v-else-if="!strategyStore.isLoading" class="empty-trades">
          <div class="empty-icon">
            <i class="fas fa-exchange-alt"></i>
          </div>
          <h4 class="empty-title">暂无交易记录</h4>
          <p class="empty-hint">{{ getNoTradesMessage() }}</p>
        </div>
      </div>
    </div>

    <!-- K线图弹窗 -->
    <div v-if="showKlineModal" class="kline-modal-overlay" @click.self="closeKlineModal">
      <div class="kline-modal">
        <div class="modal-header">
          <h3 class="modal-title">
            {{ selectedTrade?.symbol || selectedSymbol }} K线图
          </h3>
          <button @click="closeKlineModal" class="close-btn">
            <i class="fas fa-times"></i>
          </button>
        </div>
        <div class="modal-content">
          <TVKlineChart
            ref="klineChartRef"
            :data="klineDataForChart"
            :markers="tradeMarkersForKline"
            :show-legend="true"
            :show-volume="true"
            :is-loading="klineStore.isLoading"
            :symbol="selectedSymbol"
            class="kline-chart"
          />
        </div>
      </div>
    </div>

    <!-- Deep Dive Modal -->
    <TradeDeepDive
      v-if="selectedDeepDiveTrade"
      :is-open="isDeepDiveOpen"
      :symbol-code="selectedDeepDiveTrade.symbolCode"
      :symbol-name="selectedDeepDiveTrade.symbol"
      :start-date="deepDiveStartDate"
      :end-date="deepDiveEndDate"
      :trades="backtestStore.trades"
      :benchmark-code="backtestStore.lastRunParams?.benchmarkCode"
      :playback-cursor="playbackCursor"
      @close="isDeepDiveOpen = false"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useRoute, useRouter } from 'vue-router';
import { useStrategyStore } from '../store/strategyStore';
import { useKlineStore } from '../store/klineStore';
import { useBacktestStore } from '../store/backtestStore';
import { useSocketIO } from '../composables/useSocketIO';
import { useStreamingBacktest } from '../composables/useStreamingBacktest';
import {
  getStrategyDetail,
  getLatestPrices,
  getAvailableVersions,
  getStrategyQuality,
  type StrategyQualityReport,
} from '../api/backtestApi';
import EquityCurve from '../components/EquityCurve.vue';
import DrawdownChart from '../components/charts/DrawdownChart.vue';
import TradeFrequencyChart from '../components/charts/TradeFrequencyChart.vue';
import TVKlineChart from '../components/charts/TVKlineChart.vue';
import TradeTable from '../components/tables/TradeTable.vue';
import TradeDeepDive from '../components/modals/TradeDeepDive.vue';
import PositionCard from '../components/PositionCard.vue';
import PlaybackController from '../components/PlaybackController.vue';
import LoadingState from '../components/common/LoadingState.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import type { Trade, HoldingPeriod as HoldingPeriodType } from '../types/backtest';

/**
 * 策略详情页面
 * 主界面显示回测收益曲线，右侧显示持仓面板，个股K线图作为弹窗
 */

const route = useRoute();
const router = useRouter();
const strategyStore = useStrategyStore();
const klineStore = useKlineStore();
const backtestStore = useBacktestStore();
const { connect } = useSocketIO();
const { start: startBacktest, isRunning: isBacktestRunning } = useStreamingBacktest();

const klineChartRef = ref<InstanceType<typeof TVKlineChart> | null>(null);
const isTradePanelExpanded = ref(true);
const tradePanelHeight = ref(200);
const isResizing = ref(false);
const isFullscreen = ref(false);
const chartScale = ref<'linear' | 'log'>('linear');
const syncDate = ref('');
const qualityLoading = ref(false);
const qualityError = ref('');
const qualityReport = ref<StrategyQualityReport | null>(null);

const showKlineModal = ref(false);
const selectedTrade = ref<Trade | null>(null);

const equityPaneHeight = ref(400);
const drawdownPaneHeight = ref(160);
const frequencyPaneHeight = ref(160);
const isDrawdownCollapsed = ref(false);
const isFrequencyCollapsed = ref(false);
const isPaneResizing = ref(false);
const currentResizingPane = ref<string | null>(null);

const strategyId = computed(() => route.params.id as string);
const qualityStartDate = computed(() => (
  backtestStore.lastRunParams?.startDate
  || strategyStore.currentBacktestResult?.startDate
  || '2024-01-01'
));
const qualityEndDate = computed(() => (
  backtestStore.lastRunParams?.endDate
  || strategyStore.currentBacktestResult?.endDate
  || new Date().toISOString().slice(0, 10)
));
const qualityPeriod = computed(() => `${qualityStartDate.value} 至 ${qualityEndDate.value}`);
const qualityDimensions = computed(() => {
  const dimensions = qualityReport.value?.dimensions || [];
  const find = (name: string) => dimensions.find(item => item.name === name);
  return [
    { key: 'profitability', label: '信号正收益性', dimension: find('signal_profitability') },
    { key: 'long_term_validity', label: '长期有效性', dimension: find('long_term_validity') },
    { key: 'parameter_stability', label: '参数稳健性', dimension: find('parameter_stability') },
    { key: 'overfitting_risk', label: '过拟合风险', dimension: find('overfit_risk') },
  ];
});
const qualityWarningReason = computed(() => {
  if (!qualityReport.value) return 'unavailable';
  const explicit = qualityReport.value.metadata?.warning_reason;
  if (explicit) return String(explicit);
  const failed = qualityReport.value.dimensions
    .filter(item => item.detail?.error)
    .map(item => item.summary || item.name);
  if (failed.length) return failed.join('；');
  return ['C', 'D'].includes(qualityReport.value.grade)
    ? qualityReport.value.summary
    : '无';
});
const authenticity = computed(() => {
  const result = strategyStore.currentBacktestResult as any;
  const metrics = (backtestStore.metrics || result?.metrics || {}) as any;
  return {
    rejectedOrders: metrics.rejectedOrders ?? result?.rejectedOrders,
    slippageCost: metrics.slippageCost ?? result?.slippageCost,
    filterStats: metrics.filterStats ?? result?.filterStats,
    theoreticalReturn: metrics.theoreticalReturn ?? result?.theoreticalReturn,
    realisticFilledReturn: metrics.realisticFilledReturn ?? result?.realisticFilledReturn ?? metrics.totalReturn,
    filledCount: metrics.tradesCount ?? metrics.totalTrades,
  };
});
const authenticityAvailable = computed(() => (
  authenticity.value.rejectedOrders != null
  || authenticity.value.slippageCost != null
  || authenticity.value.filterStats != null
));
const rejectedOrderTotal = computed<number | null>(() => {
  const data = authenticity.value.rejectedOrders;
  if (!data) return null;
  if (Number.isFinite(data.total)) return Number(data.total);
  return Object.entries(data)
    .filter(([key, value]) => key !== 'total' && Number.isFinite(value))
    .reduce((sum, [, value]) => sum + Number(value), 0);
});
const fillRate = computed<number | null>(() => {
  const filled = authenticity.value.filledCount;
  const rejected = rejectedOrderTotal.value;
  if (!Number.isFinite(filled) || rejected == null || filled + rejected <= 0) return null;
  return filled / (filled + rejected);
});
const rejectRate = computed<number | null>(() => fillRate.value == null ? null : 1 - fillRate.value);
const theoreticalReturn = computed<number | null>(() => (
  Number.isFinite(authenticity.value.theoreticalReturn) ? authenticity.value.theoreticalReturn : null
));
const realisticFilledReturn = computed<number | null>(() => (
  authenticityAvailable.value && Number.isFinite(authenticity.value.realisticFilledReturn)
    ? authenticity.value.realisticFilledReturn
    : null
));
const rejectedReasonItems = computed(() => {
  const data = authenticity.value.rejectedOrders;
  if (!data) return [];
  const labels: Record<string, string> = {
    suspended: '停牌',
    limitUp: '涨停未开板',
    limitDown: '跌停',
    st: 'ST',
    volume: '成交量不足',
    insufficientCash: '现金不足',
    invalidPrice: '价格无效',
    other: '其他',
  };
  return Object.entries(data)
    .filter(([key, value]) => key !== 'total' && Number.isFinite(value))
    .map(([key, value]) => ({ key, label: labels[key] || key, value }));
});
const filterStatItems = computed(() => {
  const stats = authenticity.value.filterStats;
  if (!stats) return [];
  const labels: Record<string, string> = {
    slippageRate: '滑点率',
    excludeSt: '过滤 ST',
    volumeCapRatio: '成交量上限',
    minTradeAmount: '最小成交额',
  };
  return Object.entries(stats).map(([key, value]) => ({
    key,
    label: labels[key] || key,
    value: key.toLowerCase().includes('rate') || key.toLowerCase().includes('ratio')
      ? formatPercent(typeof value === 'number' ? value : null)
      : typeof value === 'boolean' ? (value ? '是' : '否') : String(value ?? 'unavailable'),
  }));
});

const hasBacktestData = computed(() => {
  // 只要有权益曲线数据就显示图表，不依赖 metrics
  return backtestStore.equitySeries.length > 0;
});

const equityCurveData = computed(() => {
  if (backtestStore.equitySeries.length > 0) {
    return [{
      versionId: 'current',
      versionName: '当前回测',
      data: backtestStore.equitySeries
    }];
  }
  return [];
});

const benchmarkData = computed(() => backtestStore.benchmarkEquitySeries);

const {
  holdingPeriods: holdingPeriodsRef,
  lastUpdated: lastUpdatedRef,
  trades: tradesRef,
  playbackMode,
  playbackCursor
} = storeToRefs(backtestStore);

const trades = computed<Trade[]>(() => {
  let baseTrades: Trade[] = backtestStore.trades;

  if (baseTrades.length === 0 && strategyStore.currentBacktestResult?.trades) {
    baseTrades = strategyStore.currentBacktestResult.trades;
  }

  if (baseTrades.length === 0 && mappedHoldingPeriods.value.length > 0) {
    baseTrades = mappedHoldingPeriods.value.flatMap(hp => {
      const arr = [];
      if (hp.entryDate) arr.push({ id: `${hp.positionId}-b`, symbol: hp.symbolName, symbolCode: hp.symbolCode, date: hp.entryDate, action: 'buy', price: hp.entryPrice || 0, quantity: hp.quantity || 0 } as Trade);
      if (hp.exitDate) arr.push({ id: `${hp.positionId}-s`, symbol: hp.symbolName, symbolCode: hp.symbolCode, date: hp.exitDate, action: 'sell', price: hp.exitPrice || 0, quantity: hp.quantity || 0, profitLoss: hp.profit } as Trade);
      return arr;
    });
  }

  if (playbackMode.value && playbackCursor.value) {
    const cursor = normalizeDate(playbackCursor.value);
    baseTrades = baseTrades.filter(t => normalizeDate(t.date || t.entryDate) <= cursor);
  }

  return baseTrades.map(trade => {
    const normalizedCode = normalizeSymbolCode(trade.symbolCode || trade.symbol);
    let displayName = trade.symbol || normalizedCode;

    if (/^\d{6}$/.test(displayName)) {
      const holding = mappedHoldingPeriods.value.find(hp => normalizeSymbolCode(hp.symbolCode) === normalizedCode);
      if (holding && holding.symbolName && !/^\d{6}$/.test(holding.symbolName)) {
        displayName = holding.symbolName;
      }
    }

    return {
      ...trade,
      date: normalizeDate(trade.date),
      entryDate: trade.entryDate ? normalizeDate(trade.entryDate) : undefined,
      exitDate: trade.exitDate ? normalizeDate(trade.exitDate) : undefined,
      symbolCode: normalizedCode,
      symbol: displayName
    };
  });
});

const selectedSymbol = computed(() => klineStore.selectedSymbol);

const klineDataForChart = computed(() => {
  const data = klineStore.klineData;
  if (playbackMode.value && playbackCursor.value) {
    const cursor = normalizeDate(playbackCursor.value);
    return data.filter(d => normalizeDate(d.date) <= cursor);
  }
  return data;
});

const tradeMarkersForKline = computed(() => {
  const targetSymbol = normalizeSymbolCode(selectedSymbol.value);
  if (!targetSymbol) return [];

  const closeMap: Record<string, number> = {};
  klineDataForChart.value.forEach(item => {
    if (item?.date && typeof item.close === 'number') {
      closeMap[item.date] = item.close;
    }
  });

  const symbolTrades = trades.value.filter(t =>
    normalizeSymbolCode(t.symbolCode || t.symbol) === targetSymbol
  );

  return symbolTrades.map(trade => ({
    date: trade.date,
    action: trade.action,
    price: closeMap[trade.date] ?? trade.price,
    quantity: trade.quantity,
    symbol: trade.symbol,
    profitLoss: trade.profitLoss,
    entryDate: trade.entryDate,
    exitDate: trade.exitDate,
    holdingDays: trade.entryDate && trade.exitDate
      ? Math.floor((new Date(trade.exitDate).getTime() - new Date(trade.entryDate).getTime()) / (1000 * 60 * 60 * 24))
      : undefined
  }));
});

type RawHoldingPeriod = HoldingPeriodType & {
  position_id?: string;
  symbol_code?: string;
  symbol_name?: string;
  entry_date?: string;
  exit_date?: string | null;
  entry_price?: number;
  exit_price?: number;
  days?: number;
  quantity?: number;
};

const rawHoldingPeriods = computed<RawHoldingPeriod[]>(() => {
  if (Array.isArray(holdingPeriodsRef.value) && holdingPeriodsRef.value.length > 0) {
    return holdingPeriodsRef.value as unknown as RawHoldingPeriod[];
  }
  const fallback = (strategyStore.currentBacktestResult as any)?.holdingPeriods;
  if (Array.isArray(fallback) && fallback.length > 0) {
    return fallback;
  }

  const allTrades = backtestStore.trades.length > 0 ? backtestStore.trades : (strategyStore.currentBacktestResult?.trades || []);
  if (allTrades.length === 0) return [];

  const sellTrades = allTrades.filter(t => t.action === 'sell');
  const holdingPeriodsMap = new Map<string, RawHoldingPeriod>();

  allTrades.forEach(trade => {
    const symbolCode = normalizeSymbolCode(trade.symbolCode || trade.symbol);
    const positionId = trade.positionId || `${symbolCode}_${trade.entryDate || trade.date}`;

    if (trade.action === 'buy') {
      if (!holdingPeriodsMap.has(positionId)) {
        holdingPeriodsMap.set(positionId, {
          positionId,
          position_id: positionId,
          symbolCode,
          symbol_code: symbolCode,
          symbolName: trade.symbol || symbolCode,
          symbol_name: trade.symbol || symbolCode,
          entryDate: trade.entryDate || trade.date,
          entry_date: trade.entryDate || trade.date,
          entryPrice: trade.entryPrice || trade.price,
          entry_price: trade.entryPrice || trade.price,
          quantity: trade.quantity,
          exitDate: null,
          exit_date: null,
          exitPrice: undefined,
          exit_price: undefined,
          profit: 0,
          holdingDays: undefined
        });
      } else {
        const existing = holdingPeriodsMap.get(positionId)!;
        existing.quantity = (existing.quantity || 0) + trade.quantity;
        if (!existing.entryDate && trade.entryDate) {
          existing.entryDate = trade.entryDate;
          existing.entry_date = trade.entryDate;
        }
      }
    }
  });

  sellTrades.forEach(trade => {
    const symbolCode = normalizeSymbolCode(trade.symbolCode || trade.symbol);
    const positionId = trade.positionId || `${symbolCode}_${trade.entryDate || trade.date}`;

    if (holdingPeriodsMap.has(positionId)) {
      const hp = holdingPeriodsMap.get(positionId)!;
      hp.exitDate = trade.exitDate || trade.date;
      hp.exit_date = trade.exitDate || trade.date;
      hp.exitPrice = trade.exitPrice || trade.price;
      hp.exit_price = trade.exitPrice || trade.price;
      hp.profit = trade.profitLoss || trade.profit_loss || 0;
      hp.holdingDays = trade.holdingDays || trade.holding_days;
    } else {
      holdingPeriodsMap.set(positionId, {
        positionId,
        position_id: positionId,
        symbolCode,
        symbol_code: symbolCode,
        symbolName: trade.symbol || symbolCode,
        symbol_name: trade.symbol || symbolCode,
        entryDate: trade.entryDate || '',
        entry_date: trade.entryDate || '',
        entryPrice: trade.entryPrice || trade.entry_price,
        entry_price: trade.entryPrice || trade.entry_price,
        quantity: trade.quantity,
        exitDate: trade.exitDate || trade.date,
        exit_date: trade.exitDate || trade.date,
        exitPrice: trade.exitPrice || trade.price,
        exit_price: trade.exitPrice || trade.price,
        profit: trade.profitLoss || trade.profit_loss || 0,
        holdingDays: trade.holdingDays || trade.holding_days
      });
    }
  });

  return Array.from(holdingPeriodsMap.values());
});

const mappedHoldingPeriods = computed<HoldingPeriodType[]>(() => {
  return rawHoldingPeriods.value.map((hp) => {
    const symbolCode = normalizeSymbolCode(hp.symbolCode || hp.symbol_code || hp.symbol || '');
    return {
      positionId: hp.positionId || hp.position_id || '',
      symbolCode,
      symbolName: hp.symbolName || hp.symbol_name || hp.symbolCode || hp.symbol_code || '',
      entryDate: hp.entryDate || hp.entry_date || (hp as any).buyDate || '',
      exitDate: hp.exitDate ?? hp.exit_date ?? (hp as any).sellDate ?? null,
      entryPrice: hp.entryPrice ?? hp.entry_price ?? undefined,
      exitPrice: hp.exitPrice ?? hp.exit_price ?? undefined,
      quantity: hp.quantity ?? (hp as any).shares ?? 0,
      profit: hp.profit ?? (hp as any).profitLoss ?? (hp as any).pnl ?? 0,
      holdingDays: hp.holdingDays ?? hp.days ?? (hp as any).holding_days ?? undefined
    };
  });
});

const currentDate = computed(() => {
  if (playbackMode.value && playbackCursor.value) return normalizeDate(playbackCursor.value);
  const endDate = backtestStore.lastRunParams?.endDate || lastUpdatedRef.value;
  if (endDate) return normalizeDate(endDate);
  return new Date().toISOString().split('T')[0];
});

const currentEquity = computed(() => {
  const date = currentDate.value;
  if (!date || backtestStore.equitySeries.length === 0) return 0;
  const point = backtestStore.equitySeries.find(p => normalizeDate(p.date) === date);
  return point ? point.equity : 0;
});

const latestPrices = ref<Record<string, number>>({});
const stockNames = ref<Record<string, string>>({});
const requestedLatestPriceSymbols = new Set<string>();

let fetchTimeout: any = null;
watch([mappedHoldingPeriods, trades, playbackCursor], ([holdings, allTrades, cursor]) => {
  if (fetchTimeout) clearTimeout(fetchTimeout);

  fetchTimeout = setTimeout(() => {
    const symbolsToFetch: string[] = [];

    holdings.forEach(hp => {
      const symbolCode = normalizeSymbolCode(hp.symbolCode || hp.symbol);
      if (symbolCode) symbolsToFetch.push(symbolCode);
    });

    allTrades.forEach(t => {
      const symbolCode = normalizeSymbolCode(t.symbolCode || t.symbol);
      if (symbolCode) symbolsToFetch.push(symbolCode);
    });

    const uniqueSymbols = [...new Set(symbolsToFetch)];

    if (uniqueSymbols.length > 0) {
      if (!playbackMode.value) {
        const filteredSymbols = uniqueSymbols.filter(symbolCode => {
          if (latestPrices.value[symbolCode] && stockNames.value[symbolCode]) return false;
          if (requestedLatestPriceSymbols.has(symbolCode)) return false;
          return true;
        });
        if (filteredSymbols.length > 0) {
          filteredSymbols.forEach(s => requestedLatestPriceSymbols.add(s));
          fetchLatestPrices(filteredSymbols);
        }
      } else {
        fetchLatestPrices(uniqueSymbols);
      }
    }
  }, 1000);
}, { immediate: true, deep: true });

async function fetchLatestPrices(symbols: string[]) {
  if (!symbols.length) return;
  try {
    let targetDate: string | undefined = playbackMode.value && playbackCursor.value
      ? normalizeDate(playbackCursor.value)
      : backtestStore.lastRunParams?.endDate;

    if (!targetDate && strategyStore.currentBacktestResult) {
      const result = strategyStore.currentBacktestResult as any;
      if (result.endDate) {
        targetDate = result.endDate;
      } else if (result.dateRange) {
        const match = String(result.dateRange).match(/~\s*(\d{4}-\d{2}-\d{2})/);
        if (match) {
          targetDate = match[1];
        }
      }
    }

    if (targetDate && !/^\d{4}-\d{2}-\d{2}$/.test(targetDate)) {
      console.warn(`[价格获取] 日期格式无效: ${targetDate}，跳过价格获取`);
      return;
    }

    if (!targetDate) {
      if (!backtestStore.running) {
        console.warn('[价格获取] 没有回测结束日期，无法获取价格');
      }
      return;
    }

    console.log(`[价格获取] 开始获取 ${symbols.length} 个标的的价格（目标日期: ${targetDate}）:`, symbols);
    const response = await getLatestPrices(symbols, targetDate);
    console.log(`[价格获取] API 响应:`, response);
    const priceMap: Record<string, number> = {};
    const nameMap: Record<string, string> = {};

    Object.entries(response || {}).forEach(([symbol, payload]) => {
      const price = (payload as any)?.price;
      const date = (payload as any)?.date;
      const name = (payload as any)?.name;
      const normalizedCode = normalizeSymbolCode(symbol);

      if (typeof price === 'number' && !Number.isNaN(price) && price > 0) {
        priceMap[normalizedCode] = price;
        console.log(`[价格获取] ${symbol}: 价格=${price}, 名称=${name}, 日期=${date}`);
      }

      if (name && normalizedCode) {
        nameMap[normalizedCode] = name;
      }
    });

    if (Object.keys(priceMap).length > 0 || Object.keys(nameMap).length > 0) {
      symbols.forEach(symbol => {
        const normalizedCode = normalizeSymbolCode(symbol);
        if (normalizedCode) {
          requestedLatestPriceSymbols.delete(normalizedCode);
        }
      });

      latestPrices.value = {
        ...latestPrices.value,
        ...priceMap
      };

      stockNames.value = {
        ...stockNames.value,
        ...nameMap
      };

      console.log(`[价格信息] 更新了 ${Object.keys(priceMap).length} 个价格和 ${Object.keys(nameMap).length} 个名称`);
    } else {
      symbols.forEach(symbol => {
        const normalizedCode = normalizeSymbolCode(symbol);
        if (normalizedCode) {
          requestedLatestPriceSymbols.delete(normalizedCode);
        }
      });
      console.warn(`[价格获取] 未能获取价格（日期: ${targetDate}）`);
    }
  } catch (error) {
    console.error('获取价格失败:', error);
    symbols.forEach(symbol => {
      const normalizedCode = normalizeSymbolCode(symbol);
      if (normalizedCode) {
        requestedLatestPriceSymbols.delete(normalizedCode);
      }
    });
  }
}

function formatPercent(value?: number | null, isDrawdown = false): string {
  if (value == null || !Number.isFinite(value)) return 'unavailable';
  if (isDrawdown) {
    return `-${Math.abs(value).toFixed(2)}%`;
  }
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

function formatScore(value?: number | null): string {
  return value == null || !Number.isFinite(value) ? 'unavailable' : value.toFixed(4);
}

function formatOptionalAmount(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return 'unavailable';
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return value.toFixed(2);
}

function handleChartHover(data: any) {
  if (data?.date) {
    syncDate.value = data.date;
  }
}

function toggleTradePanel() {
  isTradePanelExpanded.value = !isTradePanelExpanded.value;
}

function toggleDrawdownPane() {
  isDrawdownCollapsed.value = !isDrawdownCollapsed.value;
}

function toggleFrequencyPane() {
  isFrequencyCollapsed.value = !isFrequencyCollapsed.value;
}

function startPaneResize(e: MouseEvent, pane: string) {
  isPaneResizing.value = true;
  currentResizingPane.value = pane;
  document.addEventListener('mousemove', doPaneResize);
  document.addEventListener('mouseup', stopPaneResize);
  e.preventDefault();
}

function doPaneResize(e: MouseEvent) {
  if (!isPaneResizing.value || !currentResizingPane.value) return;

  const chartsContainer = document.querySelector('.charts-container');
  if (!chartsContainer) return;

  const containerRect = chartsContainer.getBoundingClientRect();
  const relativeY = e.clientY - containerRect.top;

  if (currentResizingPane.value === 'equity') {
    const newHeight = Math.max(200, Math.min(relativeY - 28, 600));
    equityPaneHeight.value = newHeight;
  }
}

function stopPaneResize() {
  isPaneResizing.value = false;
  currentResizingPane.value = null;
  document.removeEventListener('mousemove', doPaneResize);
  document.removeEventListener('mouseup', stopPaneResize);
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().then(() => {
      isFullscreen.value = true;
    });
  } else {
    document.exitFullscreen().then(() => {
      isFullscreen.value = false;
    });
  }
}

function startResizing(e: MouseEvent) {
  isResizing.value = true;
  document.addEventListener('mousemove', doResize);
  document.addEventListener('mouseup', stopResizing);
  e.preventDefault();
}

function doResize(e: MouseEvent) {
  if (!isResizing.value) return;

  const newHeight = window.innerHeight - e.clientY;

  if (newHeight > 100 && newHeight < window.innerHeight * 0.8) {
    tradePanelHeight.value = newHeight;
    isTradePanelExpanded.value = true;
  }
}

function stopResizing() {
  isResizing.value = false;
  document.removeEventListener('mousemove', doResize);
  document.removeEventListener('mouseup', stopResizing);
}

const selectedDeepDiveTrade = ref<Trade | null>(null);
const isDeepDiveOpen = ref(false);
const deepDiveStartDate = ref('');
const deepDiveEndDate = ref('');

async function handleTradeSelect(trade: Trade) {
  selectedTrade.value = trade;
  const symbolCode = normalizeSymbolCode(trade.symbolCode || trade.symbol);
  klineStore.setSelectedSymbol(symbolCode);

  // 先显示弹窗，提升用户体验，避免等待感
  selectedDeepDiveTrade.value = trade;
  isDeepDiveOpen.value = true;
  showKlineModal.value = true;

  // 弹窗显示后再异步加载数据
  if (symbolCode) {
    const entry = new Date(trade.entryDate || trade.date);
    const start = new Date(entry);
    start.setDate(start.getDate() - 60);
    const end = new Date(entry);
    end.setDate(end.getDate() + 60);

    const formatDate = (date: Date) => date.toISOString().split('T')[0];
    await klineStore.loadKlineData(symbolCode, formatDate(start), formatDate(end));
  }

  if (!symbolCode) return;

  const entry = new Date(trade.entryDate || trade.date);
  const exit = trade.exitDate ? new Date(trade.exitDate) : new Date(entry);
  if (!trade.exitDate) exit.setDate(entry.getDate() + 5);

  const start = new Date(entry);
  start.setDate(start.getDate() - 20);
  const end = new Date(exit);
  end.setDate(end.getDate() + 10);

  const formatDate = (date: Date) => date.toISOString().split('T')[0];
  deepDiveStartDate.value = formatDate(start);
  deepDiveEndDate.value = formatDate(end);
}

async function handlePositionAnalyze(pos: any) {
  const syntheticTrade: Trade = {
    id: `current-pos-${pos.symbolCode}`,
    date: pos.entryDate || currentDate.value,
    symbol: pos.symbolName,
    symbolCode: pos.symbolCode,
    action: 'buy',
    price: pos.cost,
    quantity: pos.quantity,
    entryPrice: pos.cost,
    entryDate: pos.entryDate || currentDate.value
  };

  await handleTradeSelect(syntheticTrade);
}

function closeKlineModal() {
  showKlineModal.value = false;
}

function getNoTradesMessage(): string {
  const versionId = route.params.id as string || route.params.versionId as string;

  if (!versionId || versionId === 'default') {
    return '请从总览页面选择一个有效的策略版本，或直接运行回测生成交易记录。';
  }

  if (strategyStore.error) {
    return `无法加载该策略版本的交易记录。${strategyStore.error}`;
  }

  return '该策略版本还没有交易记录，请运行回测生成交易数据。';
}

function normalizeSymbolCode(value?: string | null): string {
  if (!value) return '';
  const trimmed = value.trim().toUpperCase();
  const match = trimmed.match(/(\d+)/);
  if (match) {
    const digits = match[1];
    return digits.length < 6 ? digits.padStart(6, '0') : digits;
  }
  return trimmed;
}

function normalizeDate(dateStr?: string | null): string {
  if (!dateStr) return '';
  return dateStr.replace(/\//g, '-').split(' ')[0];
}

function runBacktest() {
  if (!strategyStore.currentVersion) {
    alert('请先选择策略');
    return;
  }

  startBacktest({
    strategy_name: strategyStore.currentVersion.name,
    start_date: '2024-01-01',
    end_date: new Date().toISOString().split('T')[0],
    benchmark_code: '000300',
    initial_capital: 1000000,
    commission: 0.0003,
    slippage: 0.001
  }, {
    onComplete: () => {
      void loadStrategyQuality();
    },
  });
}

async function loadStrategyQuality(targetStrategyId?: string) {
  const id = targetStrategyId || strategyId.value;
  if (!id) {
    qualityReport.value = null;
    qualityError.value = '';
    return;
  }
  qualityLoading.value = true;
  qualityError.value = '';
  try {
    qualityReport.value = await getStrategyQuality(
      id,
      qualityStartDate.value,
      qualityEndDate.value,
    );
  } catch (error) {
    qualityReport.value = null;
    qualityError.value = error instanceof Error ? error.message : '策略质量评估失败';
  } finally {
    qualityLoading.value = false;
  }
}

async function loadStrategyDetail() {
  let versionId = route.params.id as string || route.params.versionId as string;

  if (!versionId || versionId === 'default') {
    try {
      strategyStore.setLoading(true);
      const versions = await getAvailableVersions();
      if (versions && versions.length > 0) {
        const firstId = versions[0].id || versions[0].version || (versions[0] as any).name;
        if (firstId) {
          console.log(`[自动重定向] 无效 ID，自动跳转到策略: ${firstId}`);
          router.replace({ name: 'StrategyDetail', params: { id: firstId } });
          versionId = firstId;
        } else {
          throw new Error('无法获取有效的策略ID');
        }
      } else {
        throw new Error('系统未找到任何可用策略，请先创建或从代码库加载策略');
      }
    } catch (e) {
      console.error('自动加载策略失败:', e);
      const msg = e instanceof Error ? e.message : '请提供有效的策略版本ID';
      strategyStore.setError(msg);
      strategyStore.setLoading(false);
      return;
    }
  }

  try {
    strategyStore.setLoading(true);
    strategyStore.setError(null);

    try {
      const result = await getStrategyDetail(versionId);
      if (result) {
        strategyStore.setCurrentBacktestResult(result);
        strategyStore.setCurrentVersion(versionId);
        void loadStrategyQuality(versionId);
      } else {
        strategyStore.setError('未找到该策略版本的回测结果');
      }
    } catch (apiError) {
      const errorMessage = apiError instanceof Error ? apiError.message : '加载策略详情失败';
      strategyStore.setError(errorMessage);
      strategyStore.setLoading(false);
    }
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : '加载失败';
    strategyStore.setError(errorMessage);
  } finally {
    strategyStore.setLoading(false);
  }
}

watch(() => route.params, () => {
  loadStrategyDetail();
}, { immediate: true });

onMounted(() => {
  backtestStore.hydrateFromStorage();
  // 使用相对路径 ''，让 Socket.IO 自动使用当前域名
  // 这样 Vite 代理可以正确代理 /socket.io 请求到后端
  connect('');
});
</script>

<style scoped>
.strategy-detail-page {
  height: 100vh;
  background: #0A0A0A;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 顶部工具栏 */
.toolbar {
  height: clamp(3rem, 9vh, 3.5rem);
  padding: 0 clamp(0.5rem, 1.5vw, 0.75rem);
  background: #1E222D;
  border-bottom: 1px solid #2A2E39;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: clamp(0.25rem, 0.5vw, 0.5rem);
  flex-shrink: 0;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: clamp(0.375rem, 0.75vw, 0.5rem);
  flex: 1;
  min-width: 0;
}

.strategy-info {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.strategy-name {
  font-size: clamp(0.875rem, 1.1vw, 1rem);
  font-weight: 600;
  color: #D1D4DC;
  margin: 0;
}

.strategy-badge {
  padding: 0.125rem 0.5rem;
  font-size: 0.625rem;
  font-weight: 600;
  color: #2962FF;
  background: rgba(41, 98, 255, 0.15);
  border-radius: 0.25rem;
  text-transform: uppercase;
}

.divider {
  width: 1px;
  height: clamp(1.25rem, 3vh, 1.5rem);
  background: #2A2E39;
  flex-shrink: 0;
}

.chart-scale {
  display: flex;
  align-items: center;
  gap: 0.375rem;
}

.scale-label {
  font-size: 0.6875rem;
  color: #787B86;
}

.scale-btn {
  padding: 0.25rem 0.5rem;
  font-size: 0.6875rem;
  color: #787B86;
  background: #2A2E39;
  border: 1px solid #363A45;
  border-radius: 0.25rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.scale-btn:hover {
  color: #D1D4DC;
}

.scale-btn.active {
  color: #2962FF;
  background: rgba(41, 98, 255, 0.15);
  border-color: rgba(41, 98, 255, 0.4);
}

.toolbar-center {
  display: flex;
  align-items: center;
  gap: clamp(0.75rem, 2vw, 1.5rem);
  flex: 1;
  justify-content: center;
  flex-wrap: wrap;
}

.metric-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.125rem;
  min-width: 60px;
}

.metric-label {
  font-size: 0.625rem;
  color: #787B86;
  text-transform: uppercase;
}

.metric-value {
  font-size: 0.875rem;
  font-weight: 600;
  color: #D1D4DC;
  font-family: 'JetBrains Mono', monospace;
}

.metric-value.positive {
  color: #22c55e;
}

.metric-value.negative {
  color: #ef4444;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: clamp(0.25rem, 0.4vw, 0.375rem);
  flex: 1;
  justify-content: flex-end;
}

.run-btn {
  padding: clamp(0.375rem, 1vh, 0.5rem) clamp(0.5rem, 1vw, 0.75rem);
  font-size: clamp(0.75rem, 0.9vw, 0.875rem);
  font-weight: 500;
  color: white;
  background: #2962FF;
  border: none;
  border-radius: 0.375rem;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  gap: 0.375rem;
}

.run-btn:hover:not(:disabled) {
  background: #1E53E5;
}

.run-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.toolbar-icon-btn {
  width: 2rem;
  height: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  color: #787B86;
  background: #2A2E39;
  border: 1px solid #363A45;
  border-radius: 0.375rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.toolbar-icon-btn:hover {
  color: #D1D4DC;
  background: #363A45;
}

.toolbar-icon-btn.active {
  color: #2962FF;
  background: rgba(41, 98, 255, 0.15);
  border-color: rgba(41, 98, 255, 0.4);
}

/* 主要内容区域 */
.main-area {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}

.quality-auth-section {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  max-height: 38vh;
  overflow: auto;
  flex-shrink: 0;
  background: #2A2E39;
  border-top: 1px solid #2A2E39;
}

.report-panel {
  padding: 0.875rem;
  color: #D1D4DC;
  background: #131722;
}

.report-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}

.report-heading h3,
.report-heading p {
  margin: 0;
}

.report-heading h3 {
  font-size: 0.875rem;
}

.report-heading p {
  margin-top: 0.25rem;
  color: #787B86;
  font-size: 0.6875rem;
}

.refresh-report-btn {
  padding: 0.375rem 0.625rem;
  border: 1px solid #363A45;
  border-radius: 0.375rem;
  color: #D1D4DC;
  background: #252932;
  cursor: pointer;
}

.refresh-report-btn:disabled {
  opacity: 0.55;
  cursor: default;
}

.quality-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.5rem;
}

.quality-grid article {
  min-width: 0;
  padding: 0.625rem;
  border: 1px solid #2A2E39;
  border-radius: 0.375rem;
  background: #1E222D;
}

.quality-grid span,
.quality-grid small {
  display: block;
  color: #787B86;
  font-size: 0.625rem;
}

.quality-grid strong {
  display: block;
  margin: 0.375rem 0;
  color: #7AA2FF;
  font-size: 1rem;
}

.report-summary {
  display: grid;
  gap: 0;
  margin: 0.75rem 0 0;
}

.report-summary.two-columns {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 1rem;
}

.report-summary div {
  display: grid;
  grid-template-columns: 7rem 1fr;
  gap: 0.75rem;
  padding: 0.5rem 0;
  border-bottom: 1px solid #2A2E39;
}

.report-summary dt {
  color: #787B86;
}

.report-summary dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.reason-distribution {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 0.875rem;
  margin-top: 0.75rem;
  padding: 0.625rem;
  border-radius: 0.375rem;
  color: #A6ACB8;
  background: #1E222D;
  font-size: 0.6875rem;
}

.reason-distribution strong {
  color: #D1D4DC;
}

@media (max-width: 1100px) {
  .quality-auth-section {
    grid-template-columns: 1fr;
  }

  .quality-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.charts-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: #2A2E39;
  overflow: hidden;
}

.chart-pane {
  background: #131722;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-label {
  font-size: 0.6875rem;
  font-weight: 600;
  color: #787B86;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.pane-content {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.pane-header {
  height: 28px;
  padding: 0 0.75rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #1E222D;
  border-bottom: 1px solid #2A2E39;
  flex-shrink: 0;
  cursor: pointer;
  user-select: none;
}

.pane-header:hover {
  background: #252932;
}

.pane-header .header-left {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.collapse-icon {
  font-size: 0.625rem;
  color: #787B86;
  transition: transform 0.2s ease;
}

.pane-action-btn {
  width: 1.5rem;
  height: 1.5rem;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.625rem;
  color: #787B86;
  background: transparent;
  border: none;
  border-radius: 0.25rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.pane-action-btn:hover {
  color: #D1D4DC;
  background: #2A2E39;
}

.pane-resize-handle {
  position: absolute;
  bottom: 0;
  left: 0;
  width: 100%;
  height: 6px;
  cursor: ns-resize;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
}

.pane-resize-handle:hover {
  background: rgba(41, 98, 255, 0.1);
}

.pane-resize-indicator {
  width: 2.5rem;
  height: 2px;
  background: #2A2E39;
  border-radius: 1px;
  transition: background 0.15s ease;
}

.pane-resize-handle:hover .pane-resize-indicator {
  background: #2962FF;
}

.equity-pane {
  position: relative;
  flex: none;
  min-height: 200px;
}

.drawdown-pane,
.frequency-pane {
  flex: none;
  min-height: 32px;
  transition: height 0.2s ease;
}

/* 右侧持仓面板 */
.position-sidebar {
  width: clamp(260px, 25vw, 320px);
  flex-shrink: 0;
  border-left: 1px solid #2A2E39;
  background: #131722;
  overflow: hidden;
}

@media (max-width: 1024px) {
  .position-sidebar {
    width: 280px;
  }
}

@media (max-width: 768px) {
  .position-sidebar {
    display: none;
  }
}

/* 空状态 */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 2rem;
}

.empty-state .empty-icon {
  width: 4rem;
  height: 4rem;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #2A2E39;
  border-radius: 50%;
  margin-bottom: 1rem;
}

.empty-state .empty-icon i {
  font-size: 1.5rem;
  color: #787B86;
}

.empty-state .empty-title {
  font-size: 1.125rem;
  font-weight: 600;
  color: #D1D4DC;
  margin: 0 0 0.5rem 0;
}

.empty-state .empty-hint {
  font-size: 0.875rem;
  color: #787B86;
  margin: 0 0 1.5rem 0;
}

.empty-action-btn {
  padding: 0.75rem 1.5rem;
  font-size: 0.875rem;
  font-weight: 500;
  color: white;
  background: #2962FF;
  border: none;
  border-radius: 0.5rem;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.empty-action-btn:hover:not(:disabled) {
  background: #1E53E5;
  transform: translateY(-1px);
}

.empty-action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 底部交易记录面板 */
.trade-panel {
  background: #1E222D;
  border-top: 1px solid #2A2E39;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  position: relative;
}

.resize-handle {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 8px;
  cursor: ns-resize;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
}

.resize-handle:hover {
  background: rgba(41, 98, 255, 0.1);
}

.resize-indicator {
  width: 3rem;
  height: 3px;
  background: #2A2E39;
  border-radius: 2px;
}

.resize-handle:hover .resize-indicator {
  background: #2962FF;
}

.panel-header {
  height: 40px;
  padding: 0 1rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  cursor: pointer;
}

.panel-header:hover {
  background: #2A2E39;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.toggle-icon {
  color: #787B86;
  font-size: 0.75rem;
}

.panel-title {
  font-size: 0.8125rem;
  font-weight: 500;
  color: #D1D4DC;
  margin: 0;
}

.loading-indicator {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.75rem;
  color: #787B86;
}

.panel-content {
  flex: 1;
  overflow: hidden;
  padding: 0.75rem 1rem;
}

.error-message {
  margin-bottom: 0.75rem;
  padding: 0.5rem 0.75rem;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 0.25rem;
  font-size: 0.75rem;
  color: #f87171;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.empty-trades {
  padding: 1.5rem;
  text-align: center;
}

.empty-trades .empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2.5rem;
  height: 2.5rem;
  margin-bottom: 0.5rem;
  border-radius: 50%;
  background: #2A2E39;
  color: #787B86;
}

.empty-trades .empty-icon i {
  font-size: 1rem;
}

.empty-trades .empty-title {
  margin: 0 0 0.25rem 0;
  font-size: 0.8125rem;
  font-weight: 500;
  color: #D1D4DC;
}

.empty-trades .empty-hint {
  margin: 0;
  font-size: 0.6875rem;
  color: #787B86;
}

/* K线图弹窗 */
.kline-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 2rem;
}

.kline-modal {
  width: 90%;
  max-width: 1200px;
  min-width: 320px;
  height: 80vh;
  max-height: 800px;
  min-height: 400px;
  background: #1E222D;
  border: 1px solid #2A2E39;
  border-radius: 0.5rem;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

@media (max-width: 768px) {
  .kline-modal {
    width: 95%;
    height: 90vh;
    max-height: none;
  }
  
  .kline-modal-overlay {
    padding: 1rem;
  }
}

.modal-header {
  height: 48px;
  padding: 0 1rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #131722;
  border-bottom: 1px solid #2A2E39;
  flex-shrink: 0;
}

.modal-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: #D1D4DC;
  margin: 0;
}

.close-btn {
  width: 2rem;
  height: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #787B86;
  background: transparent;
  border: none;
  border-radius: 0.25rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.close-btn:hover {
  color: #D1D4DC;
  background: #2A2E39;
}

.modal-content {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.kline-chart {
  width: 100%;
  height: 100%;
}

/* 响应式 */
@media (max-width: 1199px) {
  .position-sidebar {
    width: 280px;
  }
}

@media (max-width: 991px) {
  .toolbar {
    flex-wrap: wrap;
    height: auto;
    min-height: 3rem;
    padding: 0.5rem;
  }

  .toolbar-left,
  .toolbar-center,
  .toolbar-right {
    width: 100%;
    justify-content: center;
  }

  .toolbar-left {
    order: 1;
    border-bottom: 1px solid #2A2E39;
    padding-bottom: 0.5rem;
    margin-bottom: 0.5rem;
  }

  .toolbar-center {
    order: 2;
    gap: 1rem;
  }

  .toolbar-right {
    order: 3;
    border-top: 1px solid #2A2E39;
    padding-top: 0.5rem;
    margin-top: 0.5rem;
  }

  .position-sidebar {
    display: none;
  }

  .drawdown-pane,
  .frequency-pane {
    height: 120px;
  }
}

@media (max-width: 767px) {
  .metric-item {
    display: none;
  }

  .metric-item:nth-child(-n+2) {
    display: flex;
  }

  .drawdown-pane,
  .frequency-pane {
    height: 100px;
  }
}
</style>
